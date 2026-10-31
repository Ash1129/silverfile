# Official 2025 Form 1040-SR draft export

The assignment requests a completed draft and a reviewable record, and prohibits filing. PDF export now fills the supplied official 2025 IRS Form 1040-SR instead of drawing a replacement summary form. It remains an unsigned review draft.

## Template and packaging

- Template: `src/silverfile/forms/templates/f1040sr_2025.pdf`, copied unchanged from the user-provided `f1040s.pdf`.
- Source metadata title: `2025 Form 1040-SR`; four pages, including the Standard Deduction Chart.
- SHA-256: `162a646c25af23338d103193f1a82a058ae61f9086b41d445edfb8672d5d0072`.
- The explicit map in `forms/official.py` rejects a different template digest or tax year.
- Package data includes the local PDF for installed packages and Docker. No runtime network download or new dependency is required.

## Populated fields and boundaries

The first four pages carry recorded names, masked identifiers, filing status, known death dates, up to four dependents, existing age/blindness boxes and supported monetary lines. Additional dependents remain in the appended recorded-facts section and set the continuation checkbox.

Amounts are copied from existing workflow lines, retaining cents; the renderer does no tax calculation. Presentation aliases are official 1a from internal 1z (the W-2-only wage profile), official 11b from 11a, and official 7a from internal 7. Address, signatures, bank details, refund/application elections and uncollected dependent attributes remain blank. Missing computations and unknown box counts remain blank rather than becoming zero.

The form stays interactive. Canonical `/AcroForm` values and widget appearances are both updated. The source XFA packet is removed from the generated copy to prevent stale viewer values. Check marks use vector appearances; nine masked SSN characters are positioned within the printed 3/2/4 groups. The original template remains unchanged.

Every official page carries a draft/not-filed mark. Monetary field tooltips identify methods and source IDs. Adjacent printed line labels link to the provenance appendix, which retains source arguments, document versions, rules, review history and escalation information. A coverage section explains aliases and uncollected fields.

Editing the downloaded PDF does not update SilverFile, calculations, audit logs or approval seals. Corrections inside SilverFile continue through existing human-review controls.

## Entry points

- **Official 1040-SR draft PDF** uses the existing `/returns/<id>/form.pdf` endpoint.
- **Review lines and sources** retains the accessible HTML review and clickable amount provenance.
- CLI paths calling `render_pdf` use the same official-layout export. Older committed trace PDFs remain historical artifacts.

## Minimal acceptance

1. Open a completed return's official draft PDF. The first four pages use the official 2025 form, including its deduction chart.
2. Case 1: 1a/1z = 18,400.00, 6b = 13,205.84, line 34 = 100.74; Single and the taxpayer age box are checked.
3. Resolved case 2 (MFJ): 12e = 36,300.00, 13b = 12,000.00, line 34 = 4,000.00; MFJ and three age/blindness boxes are checked.
4. Click a linked monetary line label to reach its source record. The HTML review remains available separately.
5. Check masked SSNs and blank signatures/address/bank/election fields. A paused return without calculations exports blank monetary fields.

`tests/test_official_pdf.py` checks actual box mappings, canonical/widget consistency, appearance streams, status checkboxes, privacy, source destinations, missing amounts, dependent continuation and template/year guards. Visual checks render the four official pages for both demos and the appendix with Poppler.

The full Windows Python 3.12 run after integration passed 613 tests. The two existing POSIX file-mode assertions in `tests/security/test_sec_keysetup.py` still fail on Windows and remain unchanged for Linux verification.
