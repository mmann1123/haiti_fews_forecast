# Repo conventions

## One-time / exploratory scripts → `experiments/`

Anything that is run once for analysis, sanity-checking, data exploration,
ad-hoc comparison, or a writeup — i.e. not part of the production sync /
dashboard pipeline — lives under `experiments/`, and each experiment gets its
own named subfolder:

```
experiments/
  <descriptive-name>/
    README.md           # one-paragraph what/why and how to run
    <scripts>.py
    output/             # plots, CSVs, etc. produced by the script
```

Do not leave one-off scripts at the repo root or inside `FEWS_Price_data/`
next to the production code — those directories are for code that runs on the
sync schedule or serves the dashboard. Production code should never `import`
from `experiments/`.
