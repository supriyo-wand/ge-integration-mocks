"""
GE Integration Mocks — a single FastAPI app hosting 8 mock services that
stand in for the systems a Great Eastern claims or underwriting workflow
would touch in production:

  /lifeasia       LifeAsia policy admin (policy lookup, proposal-form archive)
  /great-app      Great Eastern App claim intake (FNOL submission + status)
  /medishield     MOH MediShield Life electronic feed (per-hospitalisation)
  /myinfo         Singpass MyInfo identity + income verification
  /lia-medical    LIA Guide to Medical Underwriting rating lookup
  /cbs            Credit Bureau Singapore financial underwriting pull
  /feat-audit     MAS FEAT audit trail sink
  /payout         GIRO / PayNow disbursement

None of these connect to real GE, MOH, LIA, or MAS systems. They serve
static demo data seeded from data/*.json plus a small amount of in-memory
state for submissions.

Deterministic: every response for a given input is identical across calls.

Endpoints per service are listed under /docs (Swagger UI). Every service
also exposes a small liveness ping at its own root.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, date
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data"


def _load(name: str) -> Dict[str, Any]:
    with open(DATA_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


PEOPLE_BLOB = _load("people.json")
UW_BLOB = _load("underwriting_cases.json")
CLAIMS_BLOB = _load("claims_cases.json")

PEOPLE: Dict[str, Dict[str, Any]] = {p["person_id"]: p for p in PEOPLE_BLOB["people"]}
PEOPLE_BY_NRIC: Dict[str, Dict[str, Any]] = {p["nric"]: p for p in PEOPLE_BLOB["people"]}
UW_CASES: Dict[str, Dict[str, Any]] = {c["application_id"]: c for c in UW_BLOB["cases"]}
CLAIMS_CASES: Dict[str, Dict[str, Any]] = {c["claim_id"]: c for c in CLAIMS_BLOB["cases"]}


# In-force policies inferred from claims cases (policy_id -> synthesised record).
# Underwriting proposals produce future policies (see APP-73007 which links to
# an existing policy). This map is what /lifeasia serves.
def _build_policy_registry() -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    for claim in CLAIMS_BLOB["cases"]:
        pid = claim["policy_id"]
        person = PEOPLE[claim["person_id"]]
        registry[pid] = {
            "policy_id": pid,
            "person_id": claim["person_id"],
            "policyholder_nric": person["nric"],
            "policyholder_name": person["full_name"],
            "product_code": "GE-LIVE-GREAT-FLEXI",
            "product_family": "GREAT Flexi Living",
            "coverage": ["Life", "Critical Illness"],
            "sum_assured_sgd": 500000 + int(claim["policy_id"][-4:]) * 137 % 500000,
            "inception_date": claim["policy_inception_date"],
            "next_premium_due": "2026-11-30",
            "annual_premium_sgd": 4800 + int(claim["policy_id"][-4:]) * 41 % 3000,
            "status": "IN_FORCE",
            "riders": ["Multiple Pay CI"] if int(claim["policy_id"][-2:]) % 2 == 0 else [],
            "life_asia_source_ref": f"LA_POLADM.PLC_{pid}",
        }
    # Overwrite the contestability-bridge policy with its authoritative record.
    # CLM-53001 also references this policy_id, so we replace the auto-seeded
    # generic row with the real GREAT Wealth Elite proposal that P-0007 bought
    # 14 months ago.
    p7 = PEOPLE["P-0007"]
    registry["GESL-2025-0007007"] = {
        "policy_id": "GESL-2025-0007007",
        "person_id": "P-0007",
        "policyholder_nric": p7["nric"],
        "policyholder_name": p7["full_name"],
        "product_code": "GE-WEALTH-ELITE-2024",
        "product_family": "GREAT Wealth Elite",
        "coverage": ["Life", "Critical Illness", "Total Permanent Disability"],
        "sum_assured_sgd": 2000000,
        "inception_date": "2025-07-11",
        "next_premium_due": "2026-11-30",
        "annual_premium_sgd": 18400,
        "status": "IN_FORCE",
        "riders": ["Premium Waiver", "Early Critical Illness Advance"],
        "life_asia_source_ref": "LA_POLADM.PLC_GESL-2025-0007007",
    }
    return registry


POLICY_REGISTRY: Dict[str, Dict[str, Any]] = _build_policy_registry()


# ---------------------------------------------------------------------------
# In-memory state (submissions, audit trail, payouts)
# ---------------------------------------------------------------------------

_state_lock = Lock()

CLAIM_SUBMISSIONS: Dict[str, Dict[str, Any]] = {}  # claim_id -> submission record
UW_SUBMISSIONS: Dict[str, Dict[str, Any]] = {}     # application_id -> submission record
AUDIT_LEDGER: List[Dict[str, Any]] = []            # append-only FEAT trail
PAYOUTS: Dict[str, Dict[str, Any]] = {}            # payout_id -> record


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# App scaffold
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GE Integration Mocks",
    description=(
        "Mock services that stand in for the systems a Great Eastern claims or "
        "underwriting workflow would touch. Not connected to any real GE, MOH, "
        "LIA, or MAS system. Deterministic outputs seeded from static data."
    ),
    version="1.0.0",
    contact={"name": "Wand Customer Engineering"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Common health + index
# ---------------------------------------------------------------------------


@app.get("/health", tags=["_meta"])
def health() -> Dict[str, Any]:
    return {"status": "ok", "at": _now(), "services": [
        "/lifeasia", "/great-app", "/medishield", "/myinfo",
        "/lia-medical", "/cbs", "/feat-audit", "/payout",
        "/document-check", "/medical-coding", "/fraud-pool",
    ]}


@app.get("/", tags=["_meta"])
def index() -> Dict[str, Any]:
    return {
        "app": "Insurance Integration Mocks",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "services": {
            "/lifeasia": "LifeAsia policy admin (policy lookup, proposal archive)",
            "/great-app": "Customer app claim intake (FNOL submission + status)",
            "/medishield": "MOH MediShield Life electronic feed",
            "/myinfo": "Singpass MyInfo identity + income verification",
            "/lia-medical": "LIA Guide to Medical Underwriting rating lookup",
            "/cbs": "Credit Bureau Singapore financial underwriting pull",
            "/feat-audit": "MAS FEAT audit trail sink",
            "/payout": "GIRO / PayNow disbursement",
            "/document-check": "Document completeness check per claim",
            "/medical-coding": "ICD-10 + procedure code validation",
            "/fraud-pool": "Fraud scoring engine",
        },
    }


# ===========================================================================
# 1. LIFEASIA — policy admin
# ===========================================================================
LA = "/lifeasia"


@app.get(f"{LA}", tags=["lifeasia"])
def lifeasia_root() -> Dict[str, Any]:
    return {"service": "LifeAsia Policy Administration", "policies": len(POLICY_REGISTRY)}


@app.get(f"{LA}/policies/{{policy_id}}", tags=["lifeasia"])
def lifeasia_get_policy(policy_id: str) -> Dict[str, Any]:
    """Returns the LifeAsia policy record. Includes policy_age_months so the
    workflow can decide whether the claim falls within the Insurance Act
    2-year contestability window."""
    pol = POLICY_REGISTRY.get(policy_id)
    if not pol:
        raise HTTPException(404, f"policy_id {policy_id} not found")
    inception = datetime.strptime(pol["inception_date"], "%Y-%m-%d").date()
    today = date.today()
    months = round((today - inception).days / 30.4375, 1)
    in_contestability = months < 24.0
    return {
        **pol,
        "policy_age_months": months,
        "within_contestability_window": in_contestability,
        "contestability_window_ends": f"{inception.year + 2:04d}-{inception.month:02d}-{inception.day:02d}",
    }


@app.get(f"{LA}/policies", tags=["lifeasia"])
def lifeasia_list_policies_by_nric(nric: str = Query(..., description="Policyholder NRIC")) -> Dict[str, Any]:
    matches = [p for p in POLICY_REGISTRY.values() if p["policyholder_nric"] == nric]
    return {"nric": nric, "count": len(matches), "policies": matches}


@app.get(f"{LA}/proposals/{{application_id}}", tags=["lifeasia"])
def lifeasia_get_proposal(application_id: str) -> Dict[str, Any]:
    """Returns the archived proposal form as originally submitted. Used by
    the contestability re-underwriting agent to compare declared answers
    versus medical evidence on the current claim."""
    case = UW_CASES.get(application_id)
    if not case:
        raise HTTPException(404, f"application_id {application_id} not found")
    person = PEOPLE.get(case["person_id"])
    return {
        "application_id": application_id,
        "proposal_form_ref": f"LA_PROPOSAL.APP_{application_id}",
        "submitted_at": case["submitted_at"],
        "policyholder_nric": person["nric"] if person else None,
        "policyholder_name": person["full_name"] if person else None,
        "product_code": case["product_code"],
        "sum_assured_sgd": case["sum_assured_sgd"],
        "disclosures": case["disclosures"],
        "channel": case["channel"],
    }


@app.get(f"{LA}/proposals-by-policy/{{policy_id}}", tags=["lifeasia"])
def lifeasia_get_proposal_by_policy(policy_id: str) -> Dict[str, Any]:
    """Return the archived proposal form for the given in-force policy.
    Special case: policy GESL-2025-0007007 carries a synthesised proposal
    that documents what P-0007 declared 14 months ago on inception."""
    pol = POLICY_REGISTRY.get(policy_id)
    if not pol:
        raise HTTPException(404, f"policy_id {policy_id} not found")

    if policy_id == "GESL-2025-0007007":
        person = PEOPLE["P-0007"]
        return {
            "policy_id": policy_id,
            "proposal_form_ref": "LA_PROPOSAL.APP_HISTORICAL_7007",
            "submitted_at": "2025-06-28T14:12:04+08:00",
            "policyholder_nric": person["nric"],
            "policyholder_name": person["full_name"],
            "product_code": pol["product_code"],
            "sum_assured_sgd": pol["sum_assured_sgd"],
            "disclosures": {
                "smoker": False,
                "height_cm": 172,
                "weight_kg": 82,
                "bmi": 27.7,
                "existing_conditions": [],
                "prior_surgeries": [],
                "family_history": {"cardiac": True, "cancer": False, "diabetes": False, "father_mi_at_age": 68},
                "hazardous_activities": [],
                "current_medications": [],
                "form_questions": {
                    "Q1_general_health_good": "Yes",
                    "Q4_high_cholesterol_diagnosis": "No",
                    "Q7_high_blood_pressure_diagnosis": "No",
                    "Q8_diabetes_diagnosis": "No",
                    "Q11_hazardous_activities": "No",
                    "Q15_any_medical_investigation_last_5yr": "No"
                }
            },
            "channel": "Financial Representative",
        }

    # generic synthesised proposal for other policies
    person = PEOPLE.get(pol["person_id"], {})
    return {
        "policy_id": policy_id,
        "proposal_form_ref": f"LA_PROPOSAL.APP_HISTORICAL_{policy_id[-4:]}",
        "submitted_at": f"{pol['inception_date']}T10:00:00+08:00",
        "policyholder_nric": pol["policyholder_nric"],
        "policyholder_name": pol["policyholder_name"],
        "product_code": pol["product_code"],
        "sum_assured_sgd": pol["sum_assured_sgd"],
        "disclosures": {
            "smoker": bool(person.get("smoker", False)),
            "existing_conditions": [],
            "family_history": {"cardiac": False, "cancer": False, "diabetes": False},
            "form_questions": {
                "Q1_general_health_good": "Yes",
                "Q7_high_blood_pressure_diagnosis": "No",
                "Q8_diabetes_diagnosis": "No",
            },
        },
        "channel": "Great Eastern App",
    }


# ===========================================================================
# 2. GREAT EASTERN APP — claim intake
# ===========================================================================
GA = "/great-app"


class ClaimIntakePull(BaseModel):
    channel: Optional[str] = None
    include_docs: bool = True


@app.get(f"{GA}", tags=["great-app"])
def great_app_root() -> Dict[str, Any]:
    return {"service": "Great Eastern App Claim Intake", "cases_available": len(CLAIMS_CASES)}


@app.get(f"{GA}/inbox", tags=["great-app"])
def great_app_inbox(limit: int = Query(10, ge=1, le=100)) -> Dict[str, Any]:
    """Return the FNOL inbox as if just pulled from the App. Every case has
    a submitted_at set in Sep 2026. Order preserved."""
    items = list(CLAIMS_CASES.values())[:limit]
    return {
        "pulled_at": _now(),
        "channel": "Great Eastern App",
        "count": len(items),
        "items": [
            {
                "claim_id": c["claim_id"],
                "person_id": c["person_id"],
                "policy_id": c["policy_id"],
                "claim_type": c["claim_type"],
                "diagnosis_primary": c["diagnosis_primary"],
                "submitted_at": c["submitted_at"],
                "total_bill_sgd": c["total_bill_sgd"],
                "medishield_covered_sgd": c["medishield_covered_sgd"],
                "insurer_liable_sgd": c["insurer_liable_sgd"],
            }
            for c in items
        ],
    }


@app.get(f"{GA}/claims/{{claim_id}}", tags=["great-app"])
def great_app_get_claim(claim_id: str) -> Dict[str, Any]:
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    person = PEOPLE.get(case["person_id"], {})
    # strip ground_truth from response — the workflow discovers it
    public = {k: v for k, v in case.items() if k != "ground_truth"}
    return {
        **public,
        "policyholder_name": person.get("full_name"),
        "policyholder_nric": person.get("nric"),
    }


@app.post(f"{GA}/claims/{{claim_id}}/status", tags=["great-app"])
def great_app_update_status(claim_id: str, decision: str, note: Optional[str] = None) -> Dict[str, Any]:
    if claim_id not in CLAIMS_CASES:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    with _state_lock:
        CLAIM_SUBMISSIONS[claim_id] = {
            "claim_id": claim_id,
            "decision": decision,
            "note": note,
            "updated_at": _now(),
        }
    return {"acknowledged": True, "claim_id": claim_id, "decision": decision, "at": _now()}


# ===========================================================================
# 3. MEDISHIELD LIFE (MOH feed)
# ===========================================================================
MS = "/medishield"


@app.get(f"{MS}", tags=["medishield"])
def medishield_root() -> Dict[str, Any]:
    return {"service": "MOH MediShield Life Electronic Feed", "note": "Hospital-submitted claim events routed by MOH"}


@app.get(f"{MS}/coverage/{{claim_id}}", tags=["medishield"])
def medishield_coverage(claim_id: str) -> Dict[str, Any]:
    """Return the MediShield Life adjudication for a hospitalisation claim.
    In production this is the MOH-forwarded electronic notification."""
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    total = case["total_bill_sgd"]
    msh = case["medishield_covered_sgd"]
    return {
        "claim_id": claim_id,
        "mshl_ref": f"MSHL-BIN-{claim_id[-4:]}-2026",
        "provider_code": case["provider_code"],
        "provider_name": case["provider_name"],
        "admission_date": case.get("admission_date") or case.get("visit_date"),
        "total_hospital_bill_sgd": total,
        "medishield_life_share_sgd": msh,
        "medisave_deduction_sgd": round(min(600.0, total * 0.03), 2),
        "patient_out_of_pocket_before_ip_sgd": round(total - msh, 2),
        "moh_notification_ref": f"MOH-{case.get('admission_date', '2026-08-28').replace('-', '')}-{claim_id[-4:]}",
    }


# ===========================================================================
# 4. MYINFO (Singpass identity + income)
# ===========================================================================
MI = "/myinfo"


@app.get(f"{MI}", tags=["myinfo"])
def myinfo_root() -> Dict[str, Any]:
    return {"service": "Singpass MyInfo (GovTech)", "note": "Consent-based verified identity + income"}


@app.get(f"{MI}/person/{{nric}}", tags=["myinfo"])
def myinfo_get_person(nric: str) -> Dict[str, Any]:
    """Return the verified MyInfo profile for the given NRIC."""
    person = PEOPLE_BY_NRIC.get(nric)
    if not person:
        raise HTTPException(404, f"NRIC {nric} not registered in mock MyInfo")
    return {
        "nric": nric,
        "consent_granted_at": _now(),
        "verified": True,
        "full_name": person["full_name"],
        "date_of_birth": person["date_of_birth"],
        "nationality": person["nationality"],
        "residential_status": person["residential_status"],
        "marital_status": person["marital_status"],
        "occupation": person["occupation"],
        "employer": person["employer"],
        "annual_income_sgd_from_iras": person["annual_income_sgd"],
        "address": person["address"],
        "postal_code": person["postal_code"],
        "last_sync": person["myinfo_last_sync"],
        "source": "GovTech.MyInfo",
    }


# ===========================================================================
# 5. LIA MEDICAL — rating lookup
# ===========================================================================
LM = "/lia-medical"


@app.get(f"{LM}", tags=["lia-medical"])
def lia_medical_root() -> Dict[str, Any]:
    return {"service": "LIA Guide to Medical Underwriting", "edition": "2024"}


class RatingRequest(BaseModel):
    age: int
    smoker: bool
    bmi: float
    conditions: List[str] = Field(default_factory=list)
    family_history: Dict[str, Any] = Field(default_factory=dict)
    occupation_class: Optional[str] = None
    hazardous_activities: List[str] = Field(default_factory=list)


class RatingResult(BaseModel):
    loading_pct: int
    decision_class: str
    rationale: List[str]
    triggered_tables: List[str]


@app.post(f"{LM}/rate", response_model=RatingResult, tags=["lia-medical"])
def lia_medical_rate(req: RatingRequest) -> RatingResult:
    """Deterministic LIA rating engine. Applies flat loadings from LIA
    schedule; combines multiple triggers additively up to a cap; downgrades
    to POSTPONE or DECLINE at extreme thresholds."""
    loading = 0
    rationale: List[str] = []
    tables: List[str] = []

    # BMI
    if req.bmi >= 32:
        loading += 100
        rationale.append(f"BMI {req.bmi} >= 32 triggers +100% (LIA table 1)")
        tables.append("BMI_table_1")
    elif req.bmi >= 28:
        loading += 25
        rationale.append(f"BMI {req.bmi} 28-31.9 triggers +25% (LIA table 1)")
        tables.append("BMI_table_1")

    # Smoker
    if req.smoker:
        loading += 25
        rationale.append("Active smoker +25% (LIA smoker table)")
        tables.append("smoker_table")

    # Family history
    fh = req.family_history or {}
    if fh.get("cardiac") and fh.get("father_mi_at_age") and fh["father_mi_at_age"] < 60:
        loading += 50
        rationale.append("Father MI before age 60 +50% (LIA table 3 cardiac fam hx)")
        tables.append("cardiac_fam_hx_table_3")
    elif fh.get("cardiac"):
        loading += 15
        rationale.append("Family cardiac history without early onset +15%")
        tables.append("cardiac_fam_hx_table_3")

    # Conditions
    conditions_lc = [c.lower() for c in req.conditions]
    for cond in conditions_lc:
        if "diabetes" in cond:
            loading += 75
            rationale.append("Type 2 Diabetes disclosed +75% (LIA endocrine table)")
            tables.append("endocrine_table")
        if "hypertension" in cond:
            loading += 25
            rationale.append("Hypertension disclosed +25% (LIA cardiovascular table)")
            tables.append("cardiovascular_table")
        if "hypothyroid" in cond:
            loading += 25
            rationale.append("Controlled hypothyroidism +25% (endocrine)")
            tables.append("endocrine_table")
        if "fatty liver" in cond:
            loading += 25
            rationale.append("Fatty liver +25% (LIA hepatic table)")
            tables.append("hepatic_table")

    # Occupation
    if req.occupation_class in ("Class 3", "Class 4"):
        loading += 50
        rationale.append(f"Occupation {req.occupation_class} +50% (LIA occupation schedule)")
        tables.append("occupation_schedule")

    # Cap and downgrade thresholds
    if loading >= 150:
        return RatingResult(
            loading_pct=0,
            decision_class="DECLINE",
            rationale=rationale + [f"Cumulative loading {loading}% exceeds LIA table 8 acceptance cap"],
            triggered_tables=tables,
        )
    if loading >= 100:
        return RatingResult(
            loading_pct=0,
            decision_class="POSTPONE",
            rationale=rationale + [f"Cumulative loading {loading}% requires APS + re-review"],
            triggered_tables=tables,
        )
    if loading == 0 and req.bmi < 24 and not req.smoker and req.age < 40:
        return RatingResult(
            loading_pct=-15,
            decision_class="PREFERRED",
            rationale=["Clean profile, wellness-eligible, -15% preferred discount"],
            triggered_tables=["preferred_lives_schedule"],
        )
    if loading == 0:
        return RatingResult(
            loading_pct=0,
            decision_class="STANDARD",
            rationale=["Clean risk"],
            triggered_tables=[],
        )
    return RatingResult(
        loading_pct=loading,
        decision_class="STANDARD_WITH_LOADING",
        rationale=rationale,
        triggered_tables=tables,
    )


# ===========================================================================
# 6. CBS — Credit Bureau Singapore
# ===========================================================================
CBS = "/cbs"


@app.get(f"{CBS}", tags=["cbs"])
def cbs_root() -> Dict[str, Any]:
    return {"service": "Credit Bureau Singapore (mock)", "note": "Financial underwriting affordability check"}


@app.get(f"{CBS}/report/{{nric}}", tags=["cbs"])
def cbs_report(nric: str) -> Dict[str, Any]:
    person = PEOPLE_BY_NRIC.get(nric)
    if not person:
        raise HTTPException(404, f"NRIC {nric} not registered")
    income = person["annual_income_sgd"]
    # deterministic pseudo-score
    seed = sum(ord(c) for c in nric)
    score = min(2000, 1400 + (seed * 3) % 500)
    tdsr = round(min(0.55, 0.15 + (seed * 7) % 30 / 100.0), 2)
    return {
        "nric": nric,
        "credit_score": score,
        "score_grade": "AA" if score >= 1900 else ("BB" if score >= 1700 else "CC"),
        "tdsr_ratio": tdsr,
        "monthly_installments_sgd": round(income * tdsr / 12, 2),
        "outstanding_facilities": ["Home Loan", "Credit Cards"] if score < 1900 else ["Credit Cards"],
        "bankruptcy_flag": False,
        "affordability_verdict": "AFFORDABLE" if tdsr < 0.4 else "REVIEW_TDSR",
        "report_generated_at": _now(),
    }


# ===========================================================================
# 7. MAS FEAT audit sink
# ===========================================================================
FEAT = "/feat-audit"


class FeatAuditEntry(BaseModel):
    workflow: str = Field(..., description="claims | underwriting")
    case_ref: str
    decision: str
    decision_maker: str = Field(..., description="agent name or human name")
    factors: List[str] = Field(default_factory=list)
    fairness_check: Optional[Dict[str, Any]] = None
    explainability_snapshot: Optional[str] = None
    accountable_role: Optional[str] = None


@app.get(f"{FEAT}", tags=["feat-audit"])
def feat_root() -> Dict[str, Any]:
    return {
        "service": "MAS FEAT Audit Sink",
        "principles": ["Fairness", "Ethics", "Accountability", "Transparency"],
        "entries": len(AUDIT_LEDGER),
    }


@app.post(f"{FEAT}/log", tags=["feat-audit"])
def feat_log(entry: FeatAuditEntry) -> Dict[str, Any]:
    with _state_lock:
        idx = len(AUDIT_LEDGER) + 1
        rec = {"seq": idx, "logged_at": _now(), **entry.model_dump()}
        AUDIT_LEDGER.append(rec)
    return {"acknowledged": True, "seq": rec["seq"], "logged_at": rec["logged_at"]}


@app.get(f"{FEAT}/ledger", tags=["feat-audit"])
def feat_ledger(workflow: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    items = AUDIT_LEDGER
    if workflow:
        items = [e for e in items if e.get("workflow") == workflow]
    return {"count": len(items), "entries": items[-limit:]}


# ===========================================================================
# 8. PAYOUT (GIRO / PayNow)
# ===========================================================================
PAY = "/payout"


class PayoutRequest(BaseModel):
    claim_id: str
    amount_sgd: float
    beneficiary_nric: str
    channel: str = Field("PayNow", description="PayNow | GIRO | Cheque")


@app.get(f"{PAY}", tags=["payout"])
def payout_root() -> Dict[str, Any]:
    return {"service": "Disbursement router (PayNow / GIRO)", "issued": len(PAYOUTS)}


@app.post(f"{PAY}/disburse", tags=["payout"])
def payout_disburse(req: PayoutRequest) -> Dict[str, Any]:
    person = PEOPLE_BY_NRIC.get(req.beneficiary_nric)
    if not person:
        raise HTTPException(404, f"beneficiary NRIC {req.beneficiary_nric} not registered")
    with _state_lock:
        seq = len(PAYOUTS) + 1
        payout_id = f"PY-2026-{seq:05d}"
        PAYOUTS[payout_id] = {
            "payout_id": payout_id,
            "claim_id": req.claim_id,
            "amount_sgd": req.amount_sgd,
            "beneficiary_nric": req.beneficiary_nric,
            "beneficiary_name": person["full_name"],
            "channel": req.channel,
            "settlement_ref": f"{req.channel[:3].upper()}-{payout_id}",
            "expected_credit_date": "T+1 business day",
            "issued_at": _now(),
        }
    return PAYOUTS[payout_id]


@app.get(f"{PAY}/{{payout_id}}", tags=["payout"])
def payout_get(payout_id: str) -> Dict[str, Any]:
    p = PAYOUTS.get(payout_id)
    if not p:
        raise HTTPException(404, f"payout_id {payout_id} not found")
    return p


# ===========================================================================
# 9. DOCUMENT CHECK — completeness of attached FNOL documents
# ===========================================================================
DOC = "/document-check"

REQUIRED_DOCS_BY_TYPE = {
    "Hospitalisation":       ["Discharge_Summary.pdf", "Hospital_Bill_Itemised.pdf", "MediShield_Statement.pdf"],
    "Day Surgery":           ["Op_Report.pdf", "Bill.pdf"],
    "Outpatient Specialist": ["Consult_Note.pdf", "Bill.pdf"],
    "Maternity":             ["Antenatal_Report.pdf", "Bill.pdf"],
    "Personal Accident":     ["Discharge_Summary.pdf", "Bill.pdf", "Incident_Report_Keppel.pdf"],
    "Elective Surgery":      ["Pre_Auth_Request.pdf", "Ortho_Consult_Note.pdf", "MRI_Report.pdf"],
}


@app.get(f"{DOC}", tags=["document-check"])
def doc_root() -> Dict[str, Any]:
    return {"service": "Document Completeness Check"}


@app.get(f"{DOC}/{{claim_id}}", tags=["document-check"])
def doc_check(claim_id: str) -> Dict[str, Any]:
    """Verify that the FNOL submission has every document required for its
    claim type. Returns present + missing lists and a complete flag."""
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    claim_type = case.get("claim_type", "")
    required = REQUIRED_DOCS_BY_TYPE.get(claim_type, ["Bill.pdf"])
    attached = case.get("attached_documents", []) or []
    # Case-insensitive substring match: attached "Bill.pdf" satisfies required "Hospital_Bill_Itemised.pdf" only if the tokens overlap.
    def _matches(req: str, atts: List[str]) -> bool:
        req_l = req.lower()
        for a in atts:
            a_l = a.lower()
            if a_l == req_l:
                return True
            if req_l.split(".")[0] in a_l or a_l.split(".")[0] in req_l:
                return True
        return False
    present = [r for r in required if _matches(r, attached)]
    missing = [r for r in required if r not in present]
    return {
        "claim_id":         claim_id,
        "claim_type":       claim_type,
        "required_docs":    required,
        "attached_docs":    attached,
        "present":          present,
        "missing":          missing,
        "complete":         len(missing) == 0,
        "reject_reason":    None if not missing else f"Missing required documents: {', '.join(missing)}",
    }


# ===========================================================================
# 10. MEDICAL CODING — ICD-10 + procedure code validation
# ===========================================================================
MED = "/medical-coding"

# Known ICD-10 codes we recognise. All other codes come back INVALID.
KNOWN_ICD10 = {
    "I21.0": "STEMI · anterior wall", "M50.10": "Cervical spondylosis with radiculopathy",
    "Z34.03": "Antenatal care · 34 weeks", "S66.902A": "Lacerated hand · tendon injury",
    "H52.13": "Refractive error · myopia", "E10.10": "Type 1 diabetic ketoacidosis",
    "T22.311A": "Full-thickness thermal burn · forearm", "K35.80": "Acute appendicitis",
    "S82.109A": "Tibial plateau fracture", "S83.211A": "Medial meniscus tear",
}

# Procedure/procedure-family flags. LASIK on H52.13 is flagged as an
# elective/cosmetic exclusion — the workflow uses this to auto-decline.
ELECTIVE_ICD_FLAGS = {
    "H52.13": {"category": "refractive/cosmetic", "excluded_under_ip": True, "note": "LASIK/refractive surgery excluded under IP plan schedule 4.2"},
}


@app.get(f"{MED}", tags=["medical-coding"])
def med_root() -> Dict[str, Any]:
    return {"service": "Medical Coding Validator", "reference": "ICD-10 · Singapore MOH billing schema"}


@app.get(f"{MED}/validate/{{claim_id}}", tags=["medical-coding"])
def med_validate(claim_id: str) -> Dict[str, Any]:
    """Validate the ICD-10 primary code on this claim, check whether the
    procedures listed are consistent with the diagnosis, and flag any
    exclusion category the diagnosis falls under."""
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    icd = case.get("icd10_primary", "")
    procs = case.get("procedures", []) or []
    icd_known = icd in KNOWN_ICD10
    flag = ELECTIVE_ICD_FLAGS.get(icd)
    is_excluded = bool(flag and flag.get("excluded_under_ip"))
    # very light consistency check — if diagnosis mentions burn, expect skin-graft-like procedure
    consistency_note = "consistent"
    diag_low = (case.get("diagnosis_primary") or "").lower()
    proc_txt = " ".join(procs).lower()
    if "burn" in diag_low and "graft" not in proc_txt and "escharotomy" not in proc_txt:
        consistency_note = "diagnosis suggests grafting/escharotomy but not itemised in procedure list"
    if "myocardial" in diag_low and not any(k in proc_txt for k in ("angio", "pci", "stent")):
        consistency_note = "MI diagnosis without angio/PCI in procedures — review coding"
    return {
        "claim_id":                     claim_id,
        "icd10_primary":                icd,
        "icd10_description":            KNOWN_ICD10.get(icd, "unknown code"),
        "icd10_valid":                  icd_known,
        "procedures":                   procs,
        "procedures_valid":             len(procs) > 0,
        "code_consistency":             consistency_note,
        "excluded_under_policy":        is_excluded,
        "exclusion_reason":             (flag or {}).get("note"),
        "reject_reason":                None if not is_excluded else (flag or {}).get("note"),
    }


# ===========================================================================
# 11. FRAUD POOL — cross-claim scoring + watchlist
# ===========================================================================
FR = "/fraud-pool"


class FraudScoreRequest(BaseModel):
    claim_id: str


@app.get(f"{FR}", tags=["fraud-pool"])
def fraud_root() -> Dict[str, Any]:
    return {"service": "Fraud Pool", "note": "Cross-claim pattern scoring + SIU watchlist"}


@app.post(f"{FR}/score", tags=["fraud-pool"])
def fraud_score(req: FraudScoreRequest) -> Dict[str, Any]:
    """Deterministic fraud score. Uses claim + policyholder + provider
    signals to produce a 0-100 risk score with a LOW/MEDIUM/HIGH band."""
    case = CLAIMS_CASES.get(req.claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {req.claim_id} not found")

    reasons: List[str] = []
    score = 10  # baseline

    total = float(case.get("total_bill_sgd") or 0)
    ins_liable = float(case.get("insurer_liable_sgd") or 0)
    claim_type = case.get("claim_type", "")
    provider = case.get("provider_code", "")

    # Signal 1: large hospitalisation with high insurer share on a driver / high-mileage occupation
    person = PEOPLE.get(case.get("person_id", ""), {})
    if claim_type == "Hospitalisation" and total > 10000 and person.get("occupation_class") == "Class 3":
        score += 35
        reasons.append("Class 3 occupation with 4-day hospitalisation bill > SGD 10k")

    # Signal 2: repeat DKA-like or high-cost admission signals a controlled DM patient with chronic pattern
    diag = (case.get("diagnosis_primary") or "").lower()
    if "ketoacidosis" in diag or "diabetic" in diag:
        score += 20
        reasons.append("DKA hospitalisation on declared T2DM policy — cross-claim pattern flag")

    # Signal 3: elective / private-hospital cluster
    if claim_type in ("Day Surgery", "Elective Surgery") and provider in ("MEH", "GHC", "RGH"):
        score += 15
        reasons.append(f"Elective procedure at private hospital {provider}")

    # Signal 4: bill amount out of band for diagnosis
    if "myocardial" in diag and total > 60000:
        score += 5
        reasons.append("MI bill on the high end of expected range")

    # Signal 5: watchlist (deterministic hash of provider)
    watchlist_hits: List[str] = []
    if provider == "MEH":
        watchlist_hits.append("provider watchlist: repeat elective escalations flagged this quarter")
        score += 5

    band = "LOW"
    action = "PROCEED"
    if score >= 50:
        band = "HIGH"
        action = "ROUTE_TO_SIU"
    elif score >= 30:
        band = "MEDIUM"
        action = "REVIEW"

    return {
        "claim_id":                 req.claim_id,
        "fraud_score":              score,
        "risk_band":                band,
        "recommended_action":       action,
        "reasons":                  reasons,
        "watchlist_hits":           watchlist_hits,
        "similar_recent_claims":    0 if band == "LOW" else 2,
        "reject_reason":            None,
    }


# ===========================================================================
# Reset (demo housekeeping)
# ===========================================================================
@app.post("/reset", tags=["_meta"])
def reset() -> Dict[str, Any]:
    with _state_lock:
        CLAIM_SUBMISSIONS.clear()
        UW_SUBMISSIONS.clear()
        AUDIT_LEDGER.clear()
        PAYOUTS.clear()
    return {"reset": True, "at": _now()}
