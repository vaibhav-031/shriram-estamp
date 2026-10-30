"""Read-only sample checker: every PDF in a Drive folder (recursively) → real OCR API → the intake checks.

    uv run python scripts/check_samples.py <drive-folder-id>

Downloads only – never moves, renames or writes anything in Drive, never touches the ledger. Prints one line per
paper: file, pages, the ID read, state, amount, and OK or the reason it would go to Needs Review. Costs one OCR call
per page.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estamp.context import Ctx  # noqa: E402
from estamp.extract import (analyze_fields, counts_on_back_page, first_broken_pair, ids_in_reading,  # noqa: E402
                            pages_per_paper)
from estamp.google_clients import FOLDER, Google  # noqa: E402
from estamp.ocr_split import _split_local  # noqa: E402
from estamp.settings import load_settings  # noqa: E402


def pdfs(g: Google, folder: str, path: str = ""):
    for f in g.list_children(folder):
        if f["mimeType"] == FOLDER:
            yield from pdfs(g, f["id"], f"{path}{f['name']}/")
        elif f["name"].lower().endswith(".pdf") or f["mimeType"] == "application/pdf":
            yield f"{path}{f['name']}", f["id"]


def check(ctx: Ctx, name: str, data: bytes) -> list[str]:
    s = ctx.settings
    pages = _split_local(data, 1)
    reads = [ctx.ocr.read(p.data, f"{name}_p{p.page_from}") for p in pages]
    ids = [ids_in_reading(r.text, r.cert, r.fields, s, ctx.matcher, ctx.resolver) for r in reads]
    deciding = next((i for i, x in enumerate(ids) if len(x) == 1), None)
    if deciding is None:
        return [f"{name}\t{len(pages)}p\t-\t-\t-\tE105 no page has a readable certificate / serial "
                f"(API certification_no: {[(r.fields or {}).get('certification_no') for r in reads]})"]
    size = pages_per_paper(ids[deciding][0], s)
    if size == 2 and len(pages) % 2:
        return [f"{name}\t{len(pages)}p\t-\t-\t-\tE106 odd page count for a front + back paper"]
    out = []
    for k in range(0, len(pages), size):
        if size == 2:
            back = sum(counts_on_back_page(h, s) for h in ids[k + 1])
            if first_broken_pair([(len(ids[k]), back)]) is not None:
                out.append(f"{name}\tp{k + 1}-{k + 2}\t{[h.cert for h in ids[k]]}\t-\t-\tE107 pairing broken "
                           f"(front IDs {len(ids[k])}, back IDs {back}: {[h.cert for h in ids[k + 1]]})")
                continue
        r = reads[k]
        a = analyze_fields(r.fields or {}, s, ctx.matcher, lambda c: False, ctx.resolver)
        verdict = "OK" if a.ok else "REVIEW: " + "; ".join(a.issues)
        out.append(f"{name}\tp{k + 1}{'-' + str(k + size) if size == 2 else ''}\t{a.cert or '-'}\t{a.state or '-'}\t"
                   f"{a.amount or '-'}\t{verdict}")
    return out


def main() -> int:
    folder = sys.argv[1]
    s = load_settings(Path(__file__).resolve().parents[1] / "config.yaml")
    g = Google(s)
    ctx = Ctx(s, None, g)
    print("FILE\tPAGES\tID\tSTATE\tAMOUNT\tRESULT")
    ok = bad = 0
    for name, fid in sorted(pdfs(g, folder)):
        try:
            lines = check(ctx, name, g.download(fid))
        except Exception as e:
            lines = [f"{name}\t-\t-\t-\t-\tFAILED: {e}"]
        for line in lines:
            print(line, flush=True)
            ok, bad = (ok + 1, bad) if line.endswith("\tOK") else (ok, bad + 1)
    print(f"\n{ok} OK, {bad} need attention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
