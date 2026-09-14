@AGENTS.md

# Command rules — read first

**Never execute any of these. The user runs every one of them manually.**

- No installation: `npm install`, `npm ci`, `pip install`
- No builds: `next build`, `npm run build`
- No tests: `npm test`, `pytest`
- No linting or typechecking: `eslint`, `tsc`, `npm run lint`
- No application startup: `next dev`, `npm run dev`, `uvicorn`

Modify code, configuration and documentation only. When work is finished, list
the files changed and tell the user exactly which commands to run themselves.

# This is the frontend half

Presentation only. It never computes a finding — every number it shows arrives
finished from the backend.

The architecture, the two-step identity flow, where OpenAI is and is not
allowed, the source list and the cost model are all documented in the root
**`../CLAUDE.md`**. Read it before changing anything that touches the API
contract in `src/lib/api.ts`.
