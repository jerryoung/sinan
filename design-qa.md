# Design QA — 司南方案 1 运行流程工作台

## Visual truth

- Source visual: `/Users/jaryoung/.codex/generated_images/019ff157-842f-7423-988c-f1b2b3838be4/exec-226743ee-9e98-4b4a-83d6-a9311b1a6d1c.png`
- Implementation: `/Users/jaryoung/project/trader/sinan/.worktrees/live-profiles/implementation-option1-final.png`
- Combined comparison: `/Users/jaryoung/project/trader/sinan/.worktrees/live-profiles/design-qa-option1-comparison.png`
- Browser state: strategy dashboard, `combo_turtle_xsmom_x2`, shadow mode
- CSS viewport and implementation pixels: 1280 × 720, device scale 1
- Source pixels: 1487 × 1058; normalized to 1280 × 911 and top-cropped to
  1280 × 720 before side-by-side comparison

## Full-view and focused comparison

The combined image compares the same top-of-dashboard state. It also serves as
the focused comparison for the visually critical header, lifecycle status row,
primary action, metric strip, and NAV chart; those details remain readable at
the normalized size, so an additional crop was not required.

The implementation preserves the selected option's hierarchy: grouped task
navigation, compact global strategy context, three-stage operational status,
one blue primary action, continuous performance metrics, wide NAV analysis, and
holdings immediately after the chart. Text and values come from actual reports,
targets, fills, and live-profile configuration rather than mock content.

## Required fidelity surfaces

- Fonts and typography: system Chinese sans plus Source Sans fallbacks match the
  reference's compact research-terminal hierarchy. Material Symbols render as
  actual icon-font glyphs; no emoji or placeholder graphics remain.
- Spacing and layout rhythm: 208px sidebar, narrow borders, compact status row,
  wide analysis column, and reduced chart height preserve the source density.
  At the shorter 720px verification viewport the holdings title begins at the
  fold; at the 1024px design target the holdings table is visible.
- Colors and tokens: dark canvas/surfaces, muted borders, green completed state,
  blue active state, red loss values, green gain values, and blue CTA match the
  source semantics.
- Image and asset fidelity: the repository's compass and wordmark are used;
  Material Symbols provide navigation/status icons. No synthetic image asset is
  substituted.
- Copy and content: lifecycle labels, strategy identity, dates, metrics, and QMT
  profile are real platform state. Differences from the mock are intentional
  data differences.

## Comparison history

1. First option-1 pass retained option-2's coral CTA and used a taller chart.
   These were P2 fidelity differences.
2. The CTA was returned to the source's blue primary token, nested button text
   inherited white contrast, and chart height was reduced from 270px to 190px.
3. The post-fix combined comparison shows no remaining actionable P0/P1/P2
   differences. Remaining differences are real data/route availability and do
   not change the selected visual direction.

## Interaction and runtime checks

- Sidebar collapse and reopen work.
- All seven navigation destinations remain reachable under the new groups.
- `000001` resolves to `平安银行 · 个股` and renders its stock-partition K line.
- Settings → 实盘配置 exposes QMT execution and QMT data connection fields.
- The default/referenced live-profile deletion guards remain present.
- Fresh browser tab: zero console errors; only existing Streamlit sidebar-theme
  inheritance warnings are present.

final result: passed
