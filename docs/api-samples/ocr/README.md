# OCR API – captured contract (2026-10-27, dev endpoint)

Captured from 3 live calls with **synthetic** test inputs (inputs saved here; no real papers, nothing to redact).
The key and org account were read from `.env` and scrubbed from every saved file (checked: none present).

## Request

```
POST https://dev-ai.zoop.one/parsely-top-layer/v1/ocr/do
Headers: x-zoop-key: <ZOOP_OCR_API_KEY>   x-zoop-org-acc: <ZOOP_OCR_ORG_ACC>   Content-Type: application/json
Body:    {"mode":"sync","ocr_config_id":"ocrc_2f0e4e33ae22475f976e4787d62e08e2","documents":[{"data":"<base64 PDF>"}]}
```

## Response (HTTP 200)

`{"success": true, "message": …, "status_code": …, "data": {"ocr_batch_id", "ocr_transaction_id",
"provider_response": {…}, "provider_response_raw": {…}}}`

`provider_response` holds `response_code`, `message`, **`fields`** (each `{"value": …}` or `{}` when not found) and
processing metadata (OCR engine `google_vision`, confidence, latencies). **No raw OCR text is returned** – only
structured fields:

`certification_no, state_code, state_name, stamp_duty_amount, unique_doc_reference, account_reference, issued_at,
first_party, second_party, purchased_by, stamp_duty_paid_by, consideration_price, article_code,
article_description, organization_name, purpose`

| Input | HTTP | response_code / message | Fields found | Time |
|---|---|---|---|---|
| `input_traditional_front_scan.pdf` (image-only: RAJASTHAN, Rs.195, S 254201) | 200 | 212 "Missing fields: stamp_duty_amount" | `certification_no "S 254201"`, `state_code "RJ"`, `state_name "Rajasthan"` | 1.3 s |
| `input_digital_estamp.pdf` (synthetic text page with "Certificate No. : IN-UP…", "Stamp Duty Amount(Rs.) : 100") | 200 | 211 "Certificate number not found" | none | 1.5 s |
| `input_not_pdf.txt` | **422** | `{"success":false,"error":{"code":422,"type":"validation","message":"documents[0] only application/pdf, image/jpeg, image/png, image/tiff document types are accepted"}}` | – | 0.1 s |

Seen response codes: **211** certificate number not found, **212** missing fields (listed in the message and in
`metadata…check_completeness.missing_fields`). The pipeline's postprocessor is named
`extract_traditional_estamp_fields`.

## Open (needs real samples)

- The synthetic digital e-stamp returned nothing – unknown whether this config reads real digital e-stamps
  (certificate no. / Unique Doc. Reference / amount) or whether the synthetic layout is just too unlike a real one.
- Stamp duty was not found on the synthetic scan – check on a real RJ front.
- Real-sample calls need Vaibhav's go-ahead (real papers carry party names → redact before saving).
