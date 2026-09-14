# Design Notes

## What changed in my understanding

A supplier PDF is not necessarily one payable. It can contain duplicate copies, delivery or customs pages, statements, credit activity, or several invoice numbers. The payable is the smallest grounded accounting record that explains one printed obligation. The ERP total is a validation signal, not a license to invent a balancing value.

The other important distinction is pricing basis. The ERP expects net unit prices, while documents may print tax-inclusive prices. The implementation tests the printed line sum, tax summary, and gross before choosing a basis. When a rate is printed per line, it remains a line tax; when a levy is stated in the summary, it remains a header tax with its printed amount.

## Examples from the documents

`DU-03` is a multi-page logistics invoice surrounded by airway-bill and customs
material. Its invoice rows total USD 1,040.06, while a supporting page contains
an unrelated insurance amount. The summary is restricted to the page that
prints the payable total, so that insurance amount is not emitted as a header
charge. The invoice's `0% GST` remains a header tax with its printed `0.00`
amount, and the ten freight/service rows remain decomposed as line items.

`INV-09` contains customs duties, customs VAT, duty summaries, and a final
Portuguese total of EUR 37,767.32. Customs summary rows and bracket-heavy OCR
artifacts are not treated as additional invoice lines. The explicit customs VAT
amount is kept in the header only when it is not already represented by a tax
line, preventing double counting in the ERP recomputation.

`DU-08` is a German dunning letter listing a cancelled credit and a separate
invoice balance. The page does not contain enough evidence to reconstruct the
original invoice's line structure or tax placement, so it is placed in
`declined` for review rather than turning the net balance into a fabricated
payable.

`HLD-01` demonstrates why OCR confidence matters. The page visually contains
management fee, VAT, withholding, and total-payment columns, but Tesseract does
not recognize all of the intermediate amounts. The extractor keeps the printed
total when it can ground it, but does not invent missing fee or tax components
just to make the ERP total balance.

## Unseen documents

The pipeline renders each PDF page, OCRs it, classifies page and file content, extracts only values supported by text, and matches codes against indexed or exact master-data fields. Unknown supplier, buyer, PO, term, tax, date, or amount fields remain blank. A document with no reliable payable signal is declined. An ambiguous review document is withheld from `payables` rather than converted into a guessed record. Distinct printed invoice numbers are grouped into separate payables using the pages that contain each number.

This generalises because decisions are based on document evidence, arithmetic reconciliation, and master-data matches rather than a catalog of invoice-specific corrections. OCR confidence, pricing basis, tax placement, and ERP reconciliation are retained in `work/drafts.json` for review.

## Documents that cannot be solved safely

A dunning or statement letter that lists a cancelled credit and an outstanding invoice balance is not enough to reconstruct the original invoice's line structure, taxes, or master-data context. The implementation therefore places that document in `declined` with a review reason. This is preferable to treating the net balance as a new invoice or fabricating lines to make the balance book.
