"""Document text/table extraction and reference-free validation of the result.

The pipeline runs in two halves. `pdf/` and `hwp` turn a document into text and
tables; `validate`, `fields` and `crosscheck` then judge what came out, without
a reference copy to compare against.
"""
