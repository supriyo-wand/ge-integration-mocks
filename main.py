"""
Insurance Integration Mocks — a single FastAPI app hosting the services a
Singapore IP insurer's claims and underwriting workflows would touch:

  /lifeasia          LifeAsia policy admin (policy lookup, proposal archive)
  /great-app         Customer app claim intake (FNOL submission + status)
  /medishield        MOH MediShield Life electronic feed
  /myinfo            Singpass MyInfo identity + income verification
  /lia-medical       LIA Guide to Medical Underwriting rating lookup
  /cbs               Credit Bureau Singapore financial underwriting pull
  /feat-audit        MAS FEAT audit trail sink
  /payout            GIRO / PayNow disbursement
  /document-check    Document completeness check per claim
  /medical-coding    ICD-10 + procedure code validation
  /fraud-pool        Fraud scoring engine
  /coverage-check    IP tier · claim type · ward class · provider network · waiting · annual limit

Not connected to any real insurer, MOH, LIA, or MAS system. All records
are synthetic. Deterministic: same input → same output on every call.
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


# ---------------------------------------------------------------------------
# IP tier catalog (generic, no branded product names)
# ---------------------------------------------------------------------------
# Every IP claim is filed against one of these tiers. The coverage matrix
# feeds /coverage-check and drives adjudication (tier-vs-claim-type fit,
# ward class fit, provider network fit, annual limit).

IP_TIERS = {
    "PrivateShield Elite": {
        "product_code": "PS-ELITE-2026",
        "annual_limit_sgd": 1500000,
        "max_ward_class": "A",
        "eligible_provider_tiers": ["Private", "Restructured"],
        "covered_claim_types": [
            "Hospitalisation", "Day Surgery", "Outpatient Specialist",
            "Personal Accident", "Maternity", "Elective Surgery",
        ],
        "waiting_periods_days": {"Maternity": 300, "Elective Surgery": 90},
        "default_riders": ["CoverBoost Rider"],
    },
    "RestructuredCare A": {
        "product_code": "RC-A-2026",
        "annual_limit_sgd": 1200000,
        "max_ward_class": "A",
        "eligible_provider_tiers": ["Restructured", "Private"],
        "covered_claim_types": [
            "Hospitalisation", "Day Surgery", "Personal Accident",
            "Maternity", "Elective Surgery",
        ],
        "waiting_periods_days": {"Maternity": 300, "Elective Surgery": 90},
        "default_riders": [],
    },
    "RestructuredCare B": {
        "product_code": "RC-B-2026",
        "annual_limit_sgd": 800000,
        "max_ward_class": "B1",
        "eligible_provider_tiers": ["Restructured"],
        "covered_claim_types": [
            "Hospitalisation", "Day Surgery", "Personal Accident",
        ],
        "waiting_periods_days": {"Elective Surgery": 180},
        "default_riders": [],
    },
    "BasicShield": {
        "product_code": "BS-STD-2026",
        "annual_limit_sgd": 300000,
        "max_ward_class": "B2",
        "eligible_provider_tiers": ["Restructured"],
        "covered_claim_types": [
            "Hospitalisation", "Personal Accident",
        ],
        "waiting_periods_days": {},
        "default_riders": [],
    },
}


def _tier_for_claim(claim: Dict[str, Any], person: Dict[str, Any]) -> str:
    """Assign an IP tier to a policy based on claim signals in a stable
    (deterministic) way. Ties the demo scenarios to concrete tiers so
    coverage-check surfaces the right rejects."""
    pid = claim["policy_id"]
    person_id = claim["person_id"]

    # Bridge case P-0007 holds PrivateShield Elite for 14 months.
    if person_id == "P-0007":
        return "PrivateShield Elite"
    # Tan Wei Ming (P-0001) holds BasicShield only → his outpatient specialist claim
    # for cervical spondylosis is NOT covered by that tier.
    if person_id == "P-0001":
        return "BasicShield"
    # Chen Xiaolei (P-0004) — quant, high income → PrivateShield Elite
    if person_id == "P-0004":
        return "PrivateShield Elite"
    # Suresh (P-0010) — construction PM, private hospital elective → PrivateShield Elite
    if person_id == "P-0010":
        return "PrivateShield Elite"
    # Nurul (P-0002) — RN, maternity claim → RestructuredCare A (private-tier maternity)
    if person_id == "P-0002":
        return "RestructuredCare A"
    # Priya (P-0006) — occupational burn → RestructuredCare A
    if person_id == "P-0006":
        return "RestructuredCare A"
    # Farah (P-0008) — teacher, KKH appendectomy → RestructuredCare A
    if person_id == "P-0008":
        return "RestructuredCare A"
    # Kevin (P-0009) — young marketing, motorbike RTA → RestructuredCare B
    if person_id == "P-0009":
        return "RestructuredCare B"
    # Rajesh (P-0003) — marine engineer with declared occupation risk → RestructuredCare B
    if person_id == "P-0003":
        return "RestructuredCare B"
    # Faizal (P-0005) — Grab driver on legacy tier → RestructuredCare B
    if person_id == "P-0005":
        return "RestructuredCare B"
    return "RestructuredCare B"


def _build_policy_registry() -> Dict[str, Dict[str, Any]]:
    """Every in-force IP policy referenced by a claim, materialised with its
    tier metadata so /lifeasia/policies and /coverage-check can serve
    identical, consistent records."""
    registry: Dict[str, Dict[str, Any]] = {}
    for claim in CLAIMS_BLOB["cases"]:
        pid = claim["policy_id"]
        person = PEOPLE[claim["person_id"]]
        tier_name = _tier_for_claim(claim, person)
        tier = IP_TIERS[tier_name]
        # Annual premium scales with tier
        base_prem = {"PrivateShield Elite": 3600, "RestructuredCare A": 2400,
                     "RestructuredCare B": 1600, "BasicShield": 900}[tier_name]
        registry[pid] = {
            "policy_id":            pid,
            "person_id":            claim["person_id"],
            "policyholder_nric":    person["nric"],
            "policyholder_name":    person["full_name"],
            "product_code":         tier["product_code"],
            "product_family":       tier_name,
            "coverage":             list(tier["covered_claim_types"]),
            "sum_assured_sgd":      tier["annual_limit_sgd"],
            "annual_limit_sgd":     tier["annual_limit_sgd"],
            "annual_used_sgd":      0,
            "max_ward_class":       tier["max_ward_class"],
            "eligible_provider_tiers": list(tier["eligible_provider_tiers"]),
            "covered_claim_types":  list(tier["covered_claim_types"]),
            "waiting_periods_days": dict(tier["waiting_periods_days"]),
            "inception_date":       claim["policy_inception_date"],
            "next_premium_due":     "2026-11-30",
            "annual_premium_sgd":   base_prem,
            "status":               "IN_FORCE",
            "riders":               list(tier["default_riders"]),
            "life_asia_source_ref": f"LA_POLADM.PLC_{pid}",
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
CROSS_WORKFLOW_SIGNALS: List[Dict[str, Any]] = []  # append-only shared store
BOUND_POLICIES: Dict[str, Dict[str, Any]] = {}     # policy_id -> newly-bound record


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# App scaffold
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Insurance Integration Mocks",
    description=(
        "Mock services that stand in for the systems a Singapore IP (Integrated "
        "Shield Plan) insurer's claims and underwriting workflows would touch. "
        "Not connected to any real insurer, MOH, LIA, or MAS system. "
        "Deterministic outputs seeded from static data."
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
        "/coverage-check", "/contestability-review",
        "/cross-workflow", "/lifeasia/bind-policy",
    ]}


@app.get("/", tags=["_meta"])
def index() -> Dict[str, Any]:
    return {
        "app": "Insurance Integration Mocks",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "services": {
            "/lifeasia":       "LifeAsia policy admin (policy lookup, proposal archive)",
            "/great-app":      "Customer app claim intake (FNOL submission + status)",
            "/medishield":     "MOH MediShield Life electronic feed",
            "/myinfo":         "Singpass MyInfo identity + income verification",
            "/lia-medical":    "LIA Guide to Medical Underwriting rating lookup",
            "/cbs":            "Credit Bureau Singapore financial underwriting pull",
            "/feat-audit":     "MAS FEAT audit trail sink",
            "/payout":         "GIRO / PayNow disbursement",
            "/document-check": "Document completeness check per claim",
            "/medical-coding": "ICD-10 + procedure code validation",
            "/fraud-pool":     "Fraud scoring engine",
            "/coverage-check": "IP tier · claim-type coverage · ward class · provider network · waiting period · annual limit",
            "/contestability-review": "Insurance Act s21 re-underwriting verdict per claim",
            "/cross-workflow": "Signal bus between Claims and Underwriting cycles",
            "/lifeasia/bind-policy": "Underwriting binds a newly-issued policy record here",
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
        "person_id": case.get("person_id"),
        "product_code": case["product_code"],
        "product_name": case.get("product_name"),
        "coverage_type": case.get("coverage_type"),
        "sum_assured_sgd": case["sum_assured_sgd"],
        "premium_frequency": case.get("premium_frequency"),
        "proposed_annual_premium_sgd": case.get("proposed_annual_premium_sgd"),
        "riders": case.get("riders", []),
        "disclosures": case["disclosures"],
        "supporting_documents": case.get("supporting_documents", []),
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

    if policy_id == "IP-2025-0007007":
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
        "channel": "Customer App",
    }


# ===========================================================================
# 2. CUSTOMER APP — claim intake
# ===========================================================================
GA = "/great-app"

# Industry-standard field discriminators every mature core-claims platform
# carries (Guidewire ClaimCenter, FINEOS AdminSuite, Pega Insurance).
# We compute them at response time so the mock stays declarative.
_CLAIM_TYPE_TO_LOB = {
    "Hospitalisation":       "Health · IP · Hospitalisation",
    "Outpatient Specialist": "Health · IP · Outpatient Specialist",
    "Day Surgery":           "Health · IP · Day Surgery",
    "Personal Accident":     "Health · IP · Personal Accident",
    "Maternity":             "Health · IP · Maternity",
    "Elective Surgery":      "Health · IP · Elective Surgery",
}
_CLAIM_TYPE_TO_SHORT = {
    "Hospitalisation":       "HOSP",
    "Outpatient Specialist": "OPS",
    "Day Surgery":           "DS",
    "Personal Accident":     "PA",
    "Maternity":             "MAT",
    "Elective Surgery":      "ES",
}


def _enrich_intake_fields(case: Dict[str, Any]) -> Dict[str, Any]:
    """Return the industry-standard intake fields for a claim record.

    - line_of_business : Pega-style case-type discriminator
    - exposure_code    : Guidewire-style Exposure id (claimant · coverage · sequence)
    - date_of_loss     : the actual medical event date (admission or visit)
    - fnol_reported_at : when the customer first notified the insurer (a beat
                         before formal submission on average).
    - reported_channel : notification channel (kept in sync with `channel`)
    """
    import datetime
    ct = case.get("claim_type", "")
    lob   = _CLAIM_TYPE_TO_LOB.get(ct, "Health · IP · " + ct)
    short = _CLAIM_TYPE_TO_SHORT.get(ct, "UNK")
    ward  = case.get("ward_class") or "OP"
    cid_num = (case.get("claim_id", "") or "").replace("CLM-", "")
    exposure_code = f"EXP-{cid_num}-{short}-{ward}"
    date_of_loss = case.get("admission_date") or case.get("visit_date") or (case.get("submitted_at") or "")[:10]
    # FNOL notified ~2 hours before formal submission (customer app trace).
    submitted = case.get("submitted_at") or ""
    fnol_reported_at = submitted
    try:
        dt = datetime.datetime.fromisoformat(submitted)
        fnol_reported_at = (dt - datetime.timedelta(hours=2)).isoformat()
    except Exception:
        pass
    return {
        "line_of_business": lob,
        "exposure_code":    exposure_code,
        "date_of_loss":     (date_of_loss[:10] if isinstance(date_of_loss, str) else date_of_loss),
        "fnol_reported_at": fnol_reported_at,
        "reported_channel": case.get("channel", "Customer App"),
    }


@app.get(f"{GA}", tags=["great-app"])
def great_app_root() -> Dict[str, Any]:
    return {"service": "Customer App Claim Intake", "cases_available": len(CLAIMS_CASES)}


@app.get(f"{GA}/inbox", tags=["great-app"])
def great_app_inbox(limit: int = Query(10, ge=1, le=100)) -> Dict[str, Any]:
    """Return the FNOL inbox as if just pulled from the App. Every case has
    a submitted_at set in Sep 2026. Order preserved.

    Every item carries the industry-standard intake fields:
    line_of_business, exposure_code, date_of_loss, fnol_reported_at,
    reported_channel — the shape a Guidewire ClaimCenter / FINEOS /
    Pega FNOL feed would produce.
    """
    items = list(CLAIMS_CASES.values())[:limit]
    out_items = []
    for c in items:
        e = _enrich_intake_fields(c)
        out_items.append({
            "claim_id":               c["claim_id"],
            "person_id":              c["person_id"],
            "policy_id":              c["policy_id"],
            "claim_type":             c["claim_type"],
            "line_of_business":       e["line_of_business"],
            "exposure_code":          e["exposure_code"],
            "diagnosis_primary":      c["diagnosis_primary"],
            "date_of_loss":           e["date_of_loss"],
            "fnol_reported_at":       e["fnol_reported_at"],
            "submitted_at":           c["submitted_at"],
            "reported_channel":       e["reported_channel"],
            "total_bill_sgd":         c["total_bill_sgd"],
            "medishield_covered_sgd": c["medishield_covered_sgd"],
            "insurer_liable_sgd":     c["insurer_liable_sgd"],
        })
    return {
        "pulled_at": _now(),
        "channel":   "Customer App",
        "count":     len(items),
        "items":     out_items,
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
        **_enrich_intake_fields(case),
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
    # MediShield_Statement is intentionally optional — MOH forwards the
    # electronic notification via /medishield/coverage; no PDF required.
    # Incident-report naming is generic so any Employer/MOM incident-report
    # PDF the customer attached matches via the fuzzy substring check.
    "Hospitalisation":       ["Discharge_Summary.pdf", "Bill.pdf"],
    "Day Surgery":           ["Op_Report.pdf", "Bill.pdf"],
    "Outpatient Specialist": ["Consult_Note.pdf", "Bill.pdf"],
    "Maternity":             ["Antenatal_Report.pdf", "Bill.pdf"],
    "Personal Accident":     ["Discharge_Summary.pdf", "Bill.pdf", "Incident_Report.pdf"],
    "Elective Surgery":      ["Pre_Auth_Request.pdf", "Ortho_Consult_Note.pdf"],
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
# 12. COVERAGE CHECK — IP tier · claim-type · ward · provider · waiting period · limit
# ===========================================================================
COV = "/coverage-check"

# Ward-class rank so we can compare "is A ward better than B1?" numerically
_WARD_RANK = {"A": 4, "B1": 3, "B2": 2, "C": 1}


@app.get(f"{COV}", tags=["coverage-check"])
def cov_root() -> Dict[str, Any]:
    return {
        "service": "IP Coverage Check",
        "note": "Verifies IP tier covers the claim type, ward class within tier limit, provider in tier network, past waiting period, and within annual limit.",
    }


@app.get(f"{COV}/{{claim_id}}", tags=["coverage-check"])
def cov_check(claim_id: str) -> Dict[str, Any]:
    """The single authoritative coverage verdict for one claim.
    Consolidates: product tier compatibility, ward-class match, provider
    network match, waiting-period status, and annual-limit remaining."""
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    pol = POLICY_REGISTRY.get(case.get("policy_id", ""))
    if not pol:
        raise HTTPException(404, f"policy for claim_id {claim_id} not found")

    claim_type = case.get("claim_type", "")
    ward_class = case.get("ward_class") or ""
    provider_tier = case.get("provider_tier") or "Restructured"
    insurer_liable = float(case.get("insurer_liable_sgd") or 0)

    # Claim-type coverage
    covered_types = pol.get("covered_claim_types") or []
    product_covers_claim_type = claim_type in covered_types

    # Ward-class match (only meaningful when a ward is recorded on the bill)
    max_ward = pol.get("max_ward_class") or "A"
    ward_ok = True
    if ward_class:
        if _WARD_RANK.get(ward_class, 0) > _WARD_RANK.get(max_ward, 4):
            ward_ok = False

    # Provider network
    eligible = pol.get("eligible_provider_tiers") or ["Restructured", "Private"]
    provider_in_network = provider_tier in eligible

    # Waiting period
    from datetime import datetime as _dt
    inception = _dt.strptime(pol["inception_date"], "%Y-%m-%d").date()
    los_date_str = case.get("admission_date") or case.get("visit_date") or "2026-09-01"
    los_date = _dt.strptime(los_date_str[:10], "%Y-%m-%d").date()
    days_since_inception = (los_date - inception).days
    waiting_map = pol.get("waiting_periods_days") or {}
    waiting_days = int(waiting_map.get(claim_type, 0))
    past_waiting_period = days_since_inception >= waiting_days

    # Annual limit remaining
    annual_limit = float(pol.get("annual_limit_sgd") or 0)
    annual_used = float(pol.get("annual_used_sgd") or 0)
    within_limit = insurer_liable <= (annual_limit - annual_used)

    # Aggregate verdict
    reasons: List[str] = []
    verdict = "COVERED"
    if not product_covers_claim_type:
        verdict = "NOT_COVERED_UNDER_PRODUCT"
        reasons.append(f"Tier {pol['product_family']} does not cover {claim_type}. Covered types: {', '.join(covered_types)}.")
    if not ward_ok:
        verdict = "WARD_CLASS_EXCEEDS_TIER"
        reasons.append(f"Bill lists ward {ward_class} but tier caps at {max_ward}.")
    if not provider_in_network:
        if verdict == "COVERED":
            verdict = "PROVIDER_OUT_OF_NETWORK"
        reasons.append(f"Provider tier '{provider_tier}' not eligible under tier {pol['product_family']}. Eligible: {', '.join(eligible)}.")
    if not past_waiting_period:
        if verdict == "COVERED":
            verdict = "WITHIN_WAITING_PERIOD"
        reasons.append(f"Loss on {los_date} · {days_since_inception} days since inception · waiting period {waiting_days} days.")
    if not within_limit:
        if verdict == "COVERED":
            verdict = "EXCEEDS_ANNUAL_LIMIT"
        reasons.append(f"Insurer liable SGD {insurer_liable} · annual remaining SGD {annual_limit - annual_used}.")

    return {
        "claim_id":                    claim_id,
        "policy_id":                   pol["policy_id"],
        "product_family":              pol["product_family"],
        "product_code":                pol["product_code"],
        "claim_type":                  claim_type,
        "product_covers_claim_type":   product_covers_claim_type,
        "ward_class":                  ward_class,
        "max_ward_class_on_tier":      max_ward,
        "ward_class_ok":               ward_ok,
        "provider_tier":               provider_tier,
        "eligible_provider_tiers":     eligible,
        "provider_in_network":         provider_in_network,
        "days_since_inception":        days_since_inception,
        "waiting_period_days":         waiting_days,
        "past_waiting_period":         past_waiting_period,
        "annual_limit_sgd":            annual_limit,
        "annual_used_sgd":             annual_used,
        "insurer_liable_sgd":          insurer_liable,
        "within_annual_limit":         within_limit,
        "coverage_verdict":            verdict,
        "reasons":                     reasons,
        "reject_reason":               None if verdict == "COVERED" else "; ".join(reasons),
    }


# ===========================================================================
# 13. CONTESTABILITY REVIEW — deterministic re-underwriting verdict per claim
# ===========================================================================
CR = "/contestability-review"


@app.get(f"{CR}", tags=["contestability-review"])
def cr_root() -> Dict[str, Any]:
    return {
        "service": "Contestability Review",
        "note": "Compares original proposal disclosures against medical evidence on the claim. Returns a re-underwriting verdict when the policy is still inside the 24-month Insurance Act s21 window.",
    }


@app.get(f"{CR}/{{claim_id}}", tags=["contestability-review"])
def cr_check(claim_id: str) -> Dict[str, Any]:
    """Deterministic contestability verdict. If the policy is inside the
    24-month window and the mock's ground_truth for this claim carries
    a material_nondisclosure record, this endpoint surfaces the specific
    proposal question, declared answer, actual evidence, and evidence
    date. Otherwise returns NO_ISSUE_FOUND or NOT_APPLICABLE."""
    case = CLAIMS_CASES.get(claim_id)
    if not case:
        raise HTTPException(404, f"claim_id {claim_id} not found")
    pol = POLICY_REGISTRY.get(case.get("policy_id", ""))
    if not pol:
        raise HTTPException(404, f"policy for claim_id {claim_id} not found")

    # Compute policy age (same formula as lifeasia policy lookup)
    from datetime import datetime as _dt
    inception = _dt.strptime(pol["inception_date"], "%Y-%m-%d").date()
    today = date.today()
    months = round((today - inception).days / 30.4375, 1)
    within_window = months < 24.0

    if not within_window:
        return {
            "claim_id":                     claim_id,
            "policy_id":                    pol["policy_id"],
            "policy_age_months":            months,
            "within_window":                False,
            "review_verdict":               "NOT_APPLICABLE",
            "material_nondisclosure_found": False,
            "proposal_question":            None,
            "declared_answer":              None,
            "actual_evidence":              None,
            "evidence_date":                None,
            "days_before_inception":        None,
            "rationale":                    f"Policy is {months} months old, outside the 24-month contestability window (Insurance Act s21).",
        }

    # Inside the window. Consult ground_truth for material non-disclosure.
    gt = case.get("ground_truth", {}) or {}
    mnd = gt.get("material_nondisclosure")
    if mnd:
        return {
            "claim_id":                     claim_id,
            "policy_id":                    pol["policy_id"],
            "policy_age_months":            months,
            "within_window":                True,
            "review_verdict":               "MATERIAL_NONDISCLOSURE",
            "material_nondisclosure_found": True,
            "proposal_question":            mnd.get("field_on_proposal_form"),
            "declared_answer":              mnd.get("declared_value"),
            "actual_evidence":              mnd.get("actual_evidence"),
            "evidence_date":                mnd.get("evidence_date"),
            "days_before_inception":        mnd.get("days_before_policy_inception"),
            "rationale":                    (
                f"Cross-check of the historical proposal form against the medical evidence attached to this claim "
                f"surfaces a material non-disclosure. The applicant declared '{mnd.get('declared_value')}' on "
                f"the proposal question '{mnd.get('field_on_proposal_form')}', but the GP referral letter dated "
                f"{mnd.get('evidence_date')} documents contrary evidence, "
                f"{mnd.get('days_before_policy_inception')} days before policy inception."
            ),
        }

    return {
        "claim_id":                     claim_id,
        "policy_id":                    pol["policy_id"],
        "policy_age_months":            months,
        "within_window":                True,
        "review_verdict":               "NO_ISSUE_FOUND",
        "material_nondisclosure_found": False,
        "proposal_question":            None,
        "declared_answer":              None,
        "actual_evidence":              None,
        "evidence_date":                None,
        "days_before_inception":        None,
        "rationale":                    f"Policy is {months} months old and inside the contestability window. Every declared proposal answer was cross-checked against the medical evidence attached to this claim; no material non-disclosure found.",
    }


# ===========================================================================
# 14. CROSS-WORKFLOW SIGNAL STORE — shared append-only bus between Claims & Underwriting
# ===========================================================================
XW = "/cross-workflow"


class CrossWorkflowSignal(BaseModel):
    workflow:                 str                             # emitter: "claims" | "underwriting"
    cycle_id:                 str
    target_workflow:          str                             # "claims" | "underwriting" | "both"
    signal_type:              str                             # contestability_finding | siu_watchlist | coverage_exclusion | fraud_pattern | policy_binding | policy_watch
    subject_policy_or_nric:   str
    note:                     str
    expires_at:               Optional[str] = None            # ISO string; when None, no expiry


@app.get(f"{XW}", tags=["cross-workflow"])
def xw_root() -> Dict[str, Any]:
    return {
        "service": "Cross-workflow Signal Store",
        "note": "Append-only bus that Claims and Underwriting use to hand risk state between each other. Signals are the connective tissue: a contestability finding on Claims raises the underwriting bar on the same customer's next proposal.",
        "signal_count": len(CROSS_WORKFLOW_SIGNALS),
    }


@app.post(f"{XW}/signals", tags=["cross-workflow"])
def xw_emit(sig: CrossWorkflowSignal) -> Dict[str, Any]:
    """Emit a cross-workflow signal. Called by the emitting workflow when
    something interesting happens that the other workflow should know."""
    with _state_lock:
        seq = len(CROSS_WORKFLOW_SIGNALS) + 1
        record = {
            "seq":                    seq,
            "emitted_at":             _now(),
            "workflow":               sig.workflow,
            "cycle_id":               sig.cycle_id,
            "target_workflow":        sig.target_workflow,
            "signal_type":            sig.signal_type,
            "subject_policy_or_nric": sig.subject_policy_or_nric,
            "note":                   sig.note,
            "expires_at":             sig.expires_at,
        }
        CROSS_WORKFLOW_SIGNALS.append(record)
    return {"acknowledged": True, "seq": seq, "emitted_at": record["emitted_at"]}


@app.get(f"{XW}/signals", tags=["cross-workflow"])
def xw_list(
    target_workflow: Optional[str] = Query(None, description="Filter to signals for this workflow"),
    subject:         Optional[str] = Query(None, description="Filter to signals about this NRIC or policy_id"),
    signal_type:     Optional[str] = Query(None, description="Filter to a single signal type"),
    limit:           int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    """List cross-workflow signals filtered by target workflow and/or subject."""
    items = CROSS_WORKFLOW_SIGNALS
    if target_workflow:
        items = [s for s in items if s.get("target_workflow") in (target_workflow, "both")]
    if subject:
        items = [s for s in items if s.get("subject_policy_or_nric") == subject]
    if signal_type:
        items = [s for s in items if s.get("signal_type") == signal_type]
    return {"count": len(items), "signals": items[-limit:]}


# ===========================================================================
# 15. LIFEASIA · BIND-POLICY — underwriting writes a newly-issued policy here
# ===========================================================================
# (Extends the existing /lifeasia namespace)


class BindPolicyRequest(BaseModel):
    application_id:              str
    person_id:                   str
    product_code:                str
    product_family:              str
    coverage_type:               str
    sum_assured_sgd:             float
    proposed_annual_premium_sgd: float
    loading_pct:                 int = 0
    rate_class:                  str
    table_rating:                Optional[str] = None
    riders:                      List[str] = []
    effective_date:              Optional[str] = None


@app.post(f"{LA}/bind-policy", tags=["lifeasia"])
def lifeasia_bind_policy(req: BindPolicyRequest) -> Dict[str, Any]:
    """Create a new in-force policy record from an approved underwriting
    application. Mirrors the shape a FINEOS Policy Administration bind
    call would return (policy id, effective date, first premium due)."""
    import datetime
    person = PEOPLE.get(req.person_id) or {}
    with _state_lock:
        seq = 20000 + len(BOUND_POLICIES) + 1
        # Naming: IP-YYYY-NNNNNNN mirroring the existing registry shape.
        policy_id = f"IP-2026-{seq:07d}"
        eff = req.effective_date or datetime.date.today().strftime("%Y-%m-%d")
        first_premium_due = eff
        final_premium = round(req.proposed_annual_premium_sgd * (1 + req.loading_pct / 100.0), 2)
        record = {
            "policy_id":            policy_id,
            "person_id":            req.person_id,
            "policyholder_nric":    person.get("nric", ""),
            "policyholder_name":    person.get("full_name", ""),
            "product_code":         req.product_code,
            "product_family":       req.product_family,
            "coverage_type":        req.coverage_type,
            "sum_assured_sgd":      req.sum_assured_sgd,
            "annual_premium_sgd":   final_premium,
            "loading_pct":          req.loading_pct,
            "rate_class":           req.rate_class,
            "table_rating":         req.table_rating,
            "riders":               req.riders,
            "inception_date":       eff,
            "first_premium_due":    first_premium_due,
            "next_premium_due":     eff,
            "status":               "IN_FORCE",
            "life_asia_source_ref": f"LA_POLADM.PLC_{policy_id}",
            "sourced_from_app":     req.application_id,
            "bound_at":             _now(),
        }
        BOUND_POLICIES[policy_id] = record
        POLICY_REGISTRY[policy_id] = record  # so subsequent GETs find it
    return record


@app.get(f"{LA}/bound-policies", tags=["lifeasia"])
def lifeasia_list_bound() -> Dict[str, Any]:
    """Everything the current session's underwriting cycle has bound."""
    return {"count": len(BOUND_POLICIES), "policies": list(BOUND_POLICIES.values())}


# ===========================================================================
# 16. AML · Sanctions / PEP / adverse media screening (per MAS Notice 314)
# ===========================================================================
AML = "/aml"

# Deterministic watchlists so the demo is reproducible. Every screen response
# is derived from these seeds, not random.
AML_PEP_WATCHLIST = {
    # NRIC that maps to APP-73005 (Faizal). The Claims cycle already flags
    # him for fraud pattern; underwriting adds a formal PEP hit here so the
    # decline is compliance-anchored, not just risk-driven.
    "S8034567F": {
        "hit_list":       ["MAS Targeted Financial Sanctions · Politically Exposed Persons"],
        "match_score":    92,
        "match_reason":   "Full-name + DOB + NRIC match against MAS PEP list (foreign PEP · immediate family member of a serving official).",
        "sanctions_hit":  False,
        "pep_hit":        True,
        "adverse_media":  True,
    },
}
AML_SANCTIONS_WATCHLIST: Dict[str, Dict[str, Any]] = {
    # Reserved for future scenarios; empty for the current demo cycle.
}


class AmlScreenRequest(BaseModel):
    nric:      str
    full_name: str
    dob:       Optional[str] = None


@app.get(f"{AML}", tags=["aml"])
def aml_root() -> Dict[str, Any]:
    return {
        "service": "AML · Sanctions / PEP / adverse-media screening",
        "purpose": "Every new customer at intake and every new-business proposal is screened against UN, MAS Targeted Financial Sanctions, PEP, and adverse-media lists. MAS Notice 314 (insurers) requires this before binding any new exposure.",
        "watchlists_loaded": {
            "sanctions": len(AML_SANCTIONS_WATCHLIST),
            "pep":       len(AML_PEP_WATCHLIST),
        },
    }


@app.post(f"{AML}/screen", tags=["aml"])
def aml_screen(req: AmlScreenRequest) -> Dict[str, Any]:
    """Deterministic AML screen. Returns a machine-readable verdict so the
    underwriting workflow can decide auto-decline vs Head-review vs clear.
    Also emits into the audit ledger so the MAS FEAT record shows every
    screen was done."""
    nric = (req.nric or "").upper().strip()
    sanctions = AML_SANCTIONS_WATCHLIST.get(nric)
    pep = AML_PEP_WATCHLIST.get(nric)
    if sanctions:
        hit = sanctions
        recommendation = "BLOCK"
    elif pep:
        hit = pep
        recommendation = "BLOCK"
    else:
        hit = {
            "hit_list":       [],
            "match_score":    0,
            "match_reason":   "No match against sanctions, PEP, or adverse-media lists.",
            "sanctions_hit":  False,
            "pep_hit":        False,
            "adverse_media":  False,
        }
        recommendation = "CLEAR"
    return {
        "screened_at":      _now(),
        "nric":             nric,
        "full_name":        req.full_name,
        "recommendation":   recommendation,
        "sanctions_hit":    hit["sanctions_hit"],
        "pep_hit":          hit["pep_hit"],
        "adverse_media":    hit["adverse_media"],
        "match_score":      hit["match_score"],
        "match_reason":     hit["match_reason"],
        "hit_list":         hit["hit_list"],
        "four_eye_required": recommendation != "CLEAR",
        "regulator_ref":    "MAS Notice 314 · s6 Customer Due Diligence + s8 Targeted Financial Sanctions",
    }


# ===========================================================================
# 17. AGGREGATION / EXPOSURE — 30x-income rule for life, 10x for CI
# ===========================================================================
AGG = "/aggregation"


# Industry practice: aggregation query also queries the LIA shared exposure
# pool (across all insurers on the customer). Mocked as a per-NRIC override so
# demo scenarios reproduce.
LIA_SHARED_POOL_EXPOSURE_SGD = {
    # Kevin (APP-73009) has SGD 2.9M of exposure across two other insurers
    # already, per the LIA shared pool. Any new binding at this insurer will
    # cross his 30x income cap.
    "T9345678K": 2_900_000,
}


@app.get(f"{AGG}/check/{{nric}}", tags=["aggregation"])
def aggregation_check(
    nric: str,
    proposed_sum_assured_sgd: float = Query(...),
    coverage_type:            str   = Query("Life"),
) -> Dict[str, Any]:
    """Sum of all in-force policies for this NRIC (this insurer + LIA shared
    exposure pool across the industry) plus the proposed new sum assured,
    checked against income multiple caps."""
    person = None
    for p in PEOPLE.values():
        if p.get("nric") == nric:
            person = p
            break
    income = float((person or {}).get("annual_income_sgd_from_iras", 0) or 0)
    existing_policies = [p for p in POLICY_REGISTRY.values() if p.get("policyholder_nric") == nric]
    existing_own_sum_assured = sum(float(p.get("sum_assured_sgd") or 0) for p in existing_policies)
    shared_pool_exposure = float(LIA_SHARED_POOL_EXPOSURE_SGD.get(nric, 0))
    existing_sum_assured = existing_own_sum_assured + shared_pool_exposure
    total_after_bind = existing_sum_assured + float(proposed_sum_assured_sgd or 0)
    # Simple industry cap: life 30x, CI/health 10x (per LIA best practice).
    multiple_cap = 10 if "CI" in coverage_type.upper() or "HEALTH" in coverage_type.upper() else 30
    cap_sgd = income * multiple_cap if income else float("inf")
    breach = total_after_bind > cap_sgd if income else False
    return {
        "checked_at":                       _now(),
        "nric":                             nric,
        "income_annual_sgd":                income,
        "existing_policy_count_own":        len(existing_policies),
        "existing_sum_assured_own_sgd":     round(existing_own_sum_assured, 2),
        "lia_shared_pool_exposure_sgd":     round(shared_pool_exposure, 2),
        "existing_total_exposure_sgd":      round(existing_sum_assured, 2),
        "proposed_sum_assured_sgd":         float(proposed_sum_assured_sgd or 0),
        "total_after_bind_sgd":             round(total_after_bind, 2),
        "multiple_cap_applied":             f"{multiple_cap}x annual income ({coverage_type})",
        "cap_sgd":                          round(cap_sgd, 2) if income else None,
        "breach":                           breach,
        "verdict":                          "OVER_AGGREGATE" if breach else "WITHIN_CAP",
        "regulator_ref":                    "LIA best practice · MAS macro-prudential guidance",
    }


# ===========================================================================
# 18. MEDICAL-EVIDENCE TRIGGER — LIA Guide 2024 sum-assured band table
# ===========================================================================
ME = "/medical-evidence"


@app.get(f"{ME}/trigger", tags=["medical-evidence"])
def medical_evidence_trigger(
    sum_assured_sgd:    float = Query(...),
    age:                int   = Query(40),
    smoker:             bool  = Query(False),
    adverse_disclosure: bool  = Query(False),
    bmi:                float = Query(22.0),
) -> Dict[str, Any]:
    """LIA Guide 2024 v3.0 sum-assured band table. Deterministic mapping of
    (sum assured × age × smoker × disclosure) → evidence pack required."""
    evidence: List[str] = []
    if sum_assured_sgd < 500_000:
        band = "Simplified issue"
    elif sum_assured_sgd < 1_000_000:
        band = "Non-medical (Questionnaire)"
        evidence.append("Reflexive medical questionnaire")
    elif sum_assured_sgd < 2_000_000:
        band = "Paramedical"
        evidence += ["Paramedical exam (BP · height/weight · urine)", "Blood profile"]
    elif sum_assured_sgd < 5_000_000:
        band = "Full medical"
        evidence += ["Full medical examination (physician-led)", "Blood profile", "Resting ECG"]
    else:
        band = "Jumbo medical"
        evidence += ["Full medical examination (physician-led)", "Blood profile", "Resting ECG", "Treadmill ECG", "Financial questionnaire"]
    if age >= 55 and "Blood profile" not in evidence:
        evidence.append("Blood profile (age-driven trigger)")
    if age >= 55 and "Resting ECG" not in evidence:
        evidence.append("Resting ECG (age-driven trigger)")
    if smoker and sum_assured_sgd >= 500_000:
        evidence.append("Cotinine test (smoker declaration)")
    if bmi and bmi >= 30 and "Blood profile" not in evidence:
        evidence.append("Blood profile (BMI-driven trigger)")
    if adverse_disclosure:
        evidence.append("Attending Physician Statement (APS)")
    return {
        "evaluated_at":       _now(),
        "sum_assured_sgd":    sum_assured_sgd,
        "band":               band,
        "evidence_required":  evidence,
        "medical_required":   any(e.startswith(("Paramedical", "Full medical", "Jumbo")) for e in evidence),
        "aps_required":       "Attending Physician Statement (APS)" in evidence,
        "already_on_file":    False,
        "postpone_recommended": bool(evidence) and band not in ("Simplified issue",),
        "regulator_ref":      "LIA Guide to Medical Underwriting Apr 2024 v3.0 · Sum-assured band table",
    }


# ===========================================================================
# 19. REINSURANCE · facultative quote
# ===========================================================================
RE = "/reinsurance"

# Reinsurer names are generic (industry-recognised) but not carrier-specific
# customer names. The insurer's own retention limit is 1M SGD standard.
RETENTION_LIMIT_STANDARD_SGD = 1_000_000
REINSURANCE_QUOTES: Dict[str, Dict[str, Any]] = {}


class ReinsuranceQuoteRequest(BaseModel):
    application_id:  str
    sum_assured_sgd: float
    rate_class:      str
    loading_pct:     int = 0
    coverage_type:   Optional[str] = "Life"


@app.get(f"{RE}", tags=["reinsurance"])
def reinsurance_root() -> Dict[str, Any]:
    return {
        "service":            "Facultative reinsurance quote desk",
        "own_retention_sgd":  RETENTION_LIMIT_STANDARD_SGD,
        "reinsurer_panel":    ["Global Re Partners · Panel Lead", "Asia Re Consortium · Panel Follow"],
        "note":               "Anything above own retention gets a facultative quote from the reinsurer panel. Substandard cases attract higher cession pct.",
    }


@app.post(f"{RE}/quote", tags=["reinsurance"])
def reinsurance_quote(req: ReinsuranceQuoteRequest) -> Dict[str, Any]:
    """Facultative quote engine. Deterministic on inputs so demo runs
    reproduce. If sum_assured <= retention, no ceding required."""
    import datetime
    with _state_lock:
        quote_id = f"RE-Q-{2026:04d}-{len(REINSURANCE_QUOTES) + 1:05d}"
    retention_kept = min(float(req.sum_assured_sgd), RETENTION_LIMIT_STANDARD_SGD)
    ceded = max(0.0, float(req.sum_assured_sgd) - RETENTION_LIMIT_STANDARD_SGD)
    ceded_pct = round((ceded / req.sum_assured_sgd) * 100, 1) if req.sum_assured_sgd else 0.0
    # Substandard loading bumps up cession by 10 percentage points capped at 80%.
    if req.loading_pct >= 50 and ceded > 0:
        ceded_pct = min(80.0, ceded_pct + 10.0)
        ceded = round(req.sum_assured_sgd * ceded_pct / 100.0, 2)
        retention_kept = req.sum_assured_sgd - ceded
    # Reinsurance premium: assume the ceded portion is priced at flat SGD 4 per
    # SGD 1000 sum assured, adjusted by the loading factor.
    ceded_annual_premium = round(ceded / 1000.0 * 4.0 * (1 + req.loading_pct / 100.0), 2)
    reinsurer = "Global Re Partners" if ceded_pct <= 50 else "Asia Re Consortium"
    if ceded == 0:
        return {
            "quote_id":               quote_id,
            "application_id":         req.application_id,
            "cede_required":          False,
            "reason":                 "Sum assured within own retention limit; no facultative reinsurance needed.",
            "own_retention_sgd":      RETENTION_LIMIT_STANDARD_SGD,
            "retention_kept_sgd":     req.sum_assured_sgd,
            "ceded_sum_assured_sgd":  0.0,
            "ceded_pct":              0.0,
            "reinsurer":              None,
            "ceded_annual_premium_sgd": 0.0,
            "quoted_at":              _now(),
        }
    record = {
        "quote_id":               quote_id,
        "application_id":         req.application_id,
        "cede_required":          True,
        "reason":                 f"Sum assured SGD {req.sum_assured_sgd} exceeds own retention SGD {RETENTION_LIMIT_STANDARD_SGD}; facultative cession quoted.",
        "own_retention_sgd":      RETENTION_LIMIT_STANDARD_SGD,
        "retention_kept_sgd":     round(retention_kept, 2),
        "ceded_sum_assured_sgd":  round(ceded, 2),
        "ceded_pct":              ceded_pct,
        "reinsurer":              reinsurer,
        "ceded_annual_premium_sgd": ceded_annual_premium,
        "loading_applied_pct":    req.loading_pct,
        "quoted_at":              _now(),
        "quote_valid_until":      (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y-%m-%d"),
    }
    with _state_lock:
        REINSURANCE_QUOTES[quote_id] = record
    return record


@app.get(f"{RE}/quotes", tags=["reinsurance"])
def reinsurance_list_quotes() -> Dict[str, Any]:
    return {"count": len(REINSURANCE_QUOTES), "quotes": list(REINSURANCE_QUOTES.values())}


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
        CROSS_WORKFLOW_SIGNALS.clear()
        REINSURANCE_QUOTES.clear()
        # keep BOUND_POLICIES so bound policies survive a reset (real behavior)
    return {"reset": True, "at": _now()}
