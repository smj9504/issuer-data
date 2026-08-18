# Real labelled eval cases

Drop real documents here to extend the PDF eval gold set beyond the synthetic
matrix. One directory per case:

```
data/eval/<case-name>/
├── doc.pdf          # the source PDF
└── expected.json    # {"category": "...", "tables": [[["cell", ...], ...]], "paragraphs": ["..."]}
```

`python -m issuer_data eval` (or `--gold-dir <dir>`) picks these up automatically
alongside the built-in synthetic cases — no code change needed.
