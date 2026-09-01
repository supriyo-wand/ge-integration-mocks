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
    ]}


@app.get("/", tags=["_meta"])
def index() -> Dict[str, Any]:
    return {
        "app": "GE Integration Mocks",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "services": {
            "/lifeasia": "LifeAsia policy admin (policy lookup, proposal archive)",
            "/great-app": "Great Eastern App claim intake (FNOL submission)",
            "/medishield": "MOH MediShield Life electronic feed",
            "/myinfo": "Singpass MyInfo identity + income verification",
            "/lia-medical": "LIA Guide to Medical Underwriting rating lookup",
            "/cbs": "Credit Bureau Singapore financial underwriting pull",
            "/feat-audit": "MAS FEAT audit trail sink",
            "/payout": "GIRO / PayNow disbursement",
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
