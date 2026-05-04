"""One-shot smoke check that the Round May 15 frontend additions are wired
in. Cheap structural validation — does NOT exercise the live browser.
Verifies symbol presence + script tag balance + backtick parity. Intended
to be run by hand once after a frontend edit.

Usage: python diagnostics/diag_frontend_smoke_round_may15.py
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
html = (HERE / 'frontend' / 'index.html').read_text(encoding='utf-8')

opens = len(re.findall(r'<script[^>]*>', html))
closes = len(re.findall(r'</script>', html))
print(f'<script> open={opens} close={closes} balanced={opens==closes}')

ticks = html.count('`')
print(f'template-string backticks: {ticks} '
      f'(parity={"even" if ticks % 2 == 0 else "ODD-WARN"})')

required = [
    'forensicChipsHTML',
    'renderForensicLegend',
    'dismissForensicLegend',
    'FORENSIC_FLAG_DEFS',
    '_normalizeForensicFlags',
    'leadersForensicLegend',
    'legendForensicChipsDismissed',
    # The amber chip palette must reuse existing tokens (sophia's brief):
    '--yellow-text',
    '--yellow-bg',
    '--yellow-border',
    # Sort order: dealbreakerChips before forensicChips in the join
    'dealbreakerChips, forensicChips',
    # The three locked chip labels:
    'Earnings ≠ Cash',  # the U+2260 ≠ glyph in UTF-8
    'High Leverage',
    'Sudden Dilution',
]
for sym in required:
    idx = html.find(sym)
    status = f'FOUND @ {idx}' if idx >= 0 else 'MISSING'
    print(f'  {sym:50s} {status}')
