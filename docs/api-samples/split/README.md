# Split API – captured contract (2026-10-27)

Captured from 4 live calls with **synthetic** test PDFs (each page says "TEST DOCUMENT - NOT A STAMP PAPER");
no real papers or customer data, nothing redacted. Inputs and raw responses + headers are in this folder.

## Request

```
POST https://live.zoopsign.com/api/pdf-utils/split        (multipart/form-data, no auth header)
file=@<pdf>  ranges[0][startPage]=1  ranges[0][endPage]=<page count>  splitSize=<1 digital | 2 traditional>
```

## Response

| Case | HTTP | Body | Files |
|---|---|---|---|
| 4 pages, `splitSize=1` | 200 | `application/octet-stream`, **zip** (4,415 B, 0.42 s) | `response_splitSize1.zip`: `-1-1.pdf`, `-2-2.pdf`, `-3-3.pdf`, `-4-4.pdf` |
| 4 pages, `splitSize=2` | 200 | zip (2,630 B, 0.09 s) | `response_splitSize2.zip`: `-1-2.pdf`, `-3-4.pdf` (2 pages each) |
| 3 pages, `splitSize=2` | 200 | zip | `response_odd3_splitSize2.zip`: `-1-2.pdf`, **`-3-3.pdf` (1 page)** – the API does **not** reject odd counts |
| not a PDF | **500** | `application/json` | `response_not_pdf.json`: `{"success":false,"message":"Failed to parse PDF document (…): No PDF header found"}` |

- Each zip entry is one part, named `<prefix>-<from>-<to>.pdf` with 1-based page numbers; the prefix was empty
  in every call. Parse the page range from the end of the name; don't rely on zip order.
- Entries came back in page order and the page content matched (checked the certificate printed on each page).
- Headers: standard security headers + `etag`; no cookies, no tokens.

## What the adapter must do

- Send `endPage` = the real page count; check the parts cover pages 1..N exactly once (no gaps, no overlaps)
  and each part has `to - from + 1` pages.
- Treat any non-200 as a failure and surface the JSON `message`; treat a 200 that is not a zip as a failure.
- Reject an odd page count for traditional papers **before** calling (E106, Phase B) – the API will not.
