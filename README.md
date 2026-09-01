# GE Integration Mocks

Mock HTTP services that stand in for the systems a Great Eastern Life Assurance claims or underwriting workflow would touch in production. Built for the Wand STAGE demo covering NTT Data's Great Eastern engagement.

Not connected to any real GE, MOH, LIA, or MAS system. Deterministic outputs seeded from static data. Safe to leave running.

## What sits behind each path

| Path prefix | Stands in for | What the real system does |
|---|---|---|
| `/lifeasia` | LifeAsia policy admin | GE's confirmed life policy admin. Lookup in-force policies, retrieve archived proposal forms. |
| `/great-app` | Great Eastern App | Customer-facing FNOL submission channel (Singpass/Great ID login). |
| `/medishield` | MOH MediShield Life feed | MOH-centralized electronic claim notification, hospital-submitted, forwarded to insurer. |
| `/myinfo` | Singpass MyInfo | GovTech identity, IRAS income, address verification with citizen consent. |
| `/lia-medical` | LIA Guide to Medical Underwriting (2024) | Loading percentages and rating class tables the industry references. |
| `/cbs` | Credit Bureau Singapore | Credit score + TDSR affordability check for financial underwriting. |
| `/feat-audit` | MAS FEAT audit sink | Fairness / Ethics / Accountability / Transparency trail per MAS AI Risk Toolkit. |
| `/payout` | GIRO / PayNow router | Local disbursement rails. |

Health check at `/health`. Swagger UI at `/docs`. OpenAPI spec at `/openapi.json`.

## Endpoints

### LifeAsia

| Method | Path | What it does |
|---|---|---|
| GET | `/lifeasia/policies/{policy_id}` | Returns the policy record with policy age and contestability window state. |
| GET | `/lifeasia/policies?nric=...` | All in-force policies for a policyholder. |
| GET | `/lifeasia/proposals/{application_id}` | Archived proposal form for an active submission. |
| GET | `/lifeasia/proposals-by-policy/{policy_id}` | Archived proposal form for an already-issued policy (used by contestability re-underwriting). |

### Great Eastern App

| Method | Path | What it does |
|---|---|---|
| GET | `/great-app/inbox?limit=10` | Pull the FNOL inbox. |
| GET | `/great-app/claims/{claim_id}` | Read a single FNOL submission. |
| POST | `/great-app/claims/{claim_id}/status` | Push a decision back to the customer channel. |

### MediShield Life

| Method | Path | What it does |
|---|---|---|
| GET | `/medishield/coverage/{claim_id}` | MOH-forwarded electronic notification with MSHL share and hospital bill breakdown. |

### MyInfo

| Method | Path | What it does |
|---|---|---|
| GET | `/myinfo/person/{nric}` | Consent-based verified profile including IRAS income. |

### LIA Medical

| Method | Path | What it does |
|---|---|---|
| POST | `/lia-medical/rate` | Deterministic LIA rating engine. Returns loading %, decision class, triggered tables, rationale. |

### CBS

| Method | Path | What it does |
|---|---|---|
| GET | `/cbs/report/{nric}` | Credit score, TDSR ratio, affordability verdict. |

### FEAT Audit

| Method | Path | What it does |
|---|---|---|
| POST | `/feat-audit/log` | Append a decision entry to the FEAT ledger. |
| GET | `/feat-audit/ledger?workflow=claims` | Read the ledger. |

### Payout

| Method | Path | What it does |
|---|---|---|
| POST | `/payout/disburse` | Issue a payout with a settlement reference. |
| GET | `/payout/{payout_id}` | Read a payout record. |

## Cast

Ten fabricated Singapore residents (S/T prefix NRICs, MyInfo-shape profiles). Every case in both workflows references one of them by `person_id`.

| person_id | Name | Age | Occupation | Note |
|---|---|---|---|---|
| P-0001 | Tan Wei Ming | 42 | Software Architect | Clean risk |
| P-0002 | Nurul Aisyah binti Rashid | 36 | Registered Nurse | Family cardiac history |
| P-0003 | Rajesh Kumar Menon | 39 | Marine Engineer (offshore) | Elevated LFT + smoker |
| P-0004 | Chen Xiaolei | 30 | Quant Analyst | Preferred profile |
| P-0005 | Muhammad Faizal bin Osman | 45 | Grab Driver | Uncontrolled DM + HTN |
| P-0006 | Priya Nair | 34 | Sous Chef | Occupational burn history |
| P-0007 | Ong Boon Heng | 51 | F&B Business Owner | Contestability bridge case |
| P-0008 | Farah Latifah binte Ismail | 40 | Teacher | Controlled hypothyroid |
| P-0009 | Kevin Lim Zhi Wei | 28 | Marketing Manager | Young clean |
| P-0010 | Suresh Ramachandran | 47 | Construction PM | Elevated CK, BMI 29.4 |

## The contestability bridge

Both workflows share the same ten policyholders. One customer, Ong Boon Heng, appears in both:

- **Underwriting workflow**: submits `APP-73007` in Sep 2026 for a spouse-life rider top-up on his existing policy.
- **Claims workflow**: submits `CLM-53001` on 2026-09-01 for a STEMI hospitalisation at Raffles Hospital. The claim is against policy `GESL-2025-0007007`, bound on 2025-07-11.

The claims workflow's policy lookup returns `policy_age_months: 13.7`, `within_contestability_window: true`. That triggers the workflow's contestability branch. The branch pulls the historical proposal from `/lifeasia/proposals-by-policy/GESL-2025-0007007` and the GP referral letter from the claim, cross-checks Q7 (high blood pressure diagnosis, declared "No") against the GP note from 2025-03-14 documenting BP 148/94 and stage 1 hypertension — 119 days before policy inception.

Wand surfaces material non-disclosure. Decision: contestability decline + policy reformation. FEAT audit entry generated.

Neither workflow announces the bridge. The Wand agent discovers it from the policy age check.

## Run locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs.

## Deploy on Render

Push to GitHub. Render picks up `render.yaml` and auto-deploys the Python service on the free tier. First deploy takes about two minutes, subsequent deploys under a minute.

The free tier sleeps after 15 minutes of inactivity. First hit after sleep spins the container up in about 30 seconds. All in-memory state (audit ledger, payouts, claim status pushes) resets on wake — the seeded case data comes back from `data/*.json` every time.

Any consumer can hit `/reset` to clear the audit ledger and payouts between demo runs.

## Reference data

- LIA Guide to Medical Underwriting for Life Insurance, 2024 edition
- LIA Code of Life Insurance Practice
- MAS FEAT Principles (2018) + MAS AI Risk Management Toolkit (Mar 2026)
- Insurance Act (Cap 142) s21 — 2-year contestability window
- MediShield Life scheme via MOH centralized electronic system
