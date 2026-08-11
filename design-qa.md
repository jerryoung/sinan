# Design QA — 司南策略研究工作台

## Visual sources

- Selected source visual: `/Users/jaryoung/.codex/generated_images/019ff157-842f-7423-988c-f1b2b3838be4/exec-e5310a4e-77e8-4273-a841-1b8aad50a241.png`
- Final implementation screenshot: `/private/tmp/sinan-ui-dashboard-final.png`
- Side-by-side comparison: `/private/tmp/sinan-design-qa-comparison.png`
- Browser state: `http://localhost:8502/`, strategy `combo_turtle_xsmom_x2`, shadow mode
- Comparison viewport: 1440 × 1024 CSS pixels
- Screenshot dimensions / density: 1440 × 1024 physical pixels, 1×

## Comparison evidence

The source and implementation were resized to the same 1440 × 1024 canvas and
placed side by side before judging. The final implementation matches the chosen
direction in information hierarchy, dark palette, analysis-to-decision-rail
ratio, compact borders, metric density, holdings table, and coral primary action.
Content differences are intentional real-state differences: the implementation
shows the selected strategy ID, target generation time, configured risk limits,
and the strategy's actual `local_qmt` profile instead of mock values.

## Iteration history

1. Captured all seven existing Streamlit pages before redesign.
2. Generated three directions and selected option 2, “策略研究工作台”.
3. Implemented shared theme, Material Symbols navigation, workflow bar, research
   canvas, decision rail, settings forms, and data-page metric strips.
4. First browser pass found the primary action below the 1024px fold, a white
   legacy report iframe, and long warehouse metrics colliding.
5. Tightened decision-rail rows, reduced chart height, added robust legacy-report
   theming, and made metric values fluid with overflow protection.
6. Final side-by-side pass aligned the high-attention action with the source's
   coral treatment and confirmed no remaining P0, P1, or P2 visual differences.

## Interaction and responsive checks

- All seven navigation destinations load and preserve the shared shell.
- Settings tabs switch correctly.
- Data-source selector exposes `sina`, `akshare`, `tushare`, and `qmt`.
- Live-profile selector exposes “新增配置”; the default `local_qmt` delete action
  is disabled.
- Strategy configuration selects a named live profile and does not expose inline
  QMT parameters.
- Backtest legacy HTML renders with the dark surface (`rgb(17, 26, 37)`).
- Main CTA is fully visible at 1440 × 1024 and uses `rgb(240, 93, 100)`.
- 1024 × 768 check has no horizontal overflow (`scrollWidth == innerWidth`).
- Fresh final browser tab reports zero console errors. Remaining warnings are
  Streamlit theme-inheritance notices and do not affect behavior or rendering.

final result: passed
