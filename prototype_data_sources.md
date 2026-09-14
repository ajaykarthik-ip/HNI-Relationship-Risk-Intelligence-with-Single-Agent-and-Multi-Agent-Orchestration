# Prototype Data Sources

## Dropped for now

- **People Data Labs**: Requires work email
- **Altrata**: Sales controlled
- **PitchBook**: Sales controlled
- **BoardEx**: Sales controlled
- **RelSci**: Sales controlled, trial requires payment details
- **GDELT**: Dropped for now
- **Companies House**: Dropped for now
- **OpenCorporates**: Dropped for now

## Using Now

### Firecrawl 🌐

- Search and scrape the public web for investments, business relationships, recent events, and negative news.
- **API**

### SEC EDGAR 🇺🇸

- Verify US company, executive, insider ownership, and filing information.
- **API, no API key required**

### Free, No-Key Sources Already in the Local Claude Code Setup

These are the free sources currently working in the local project without API keys:

| Service | Endpoints | Status |
|---|---|---|
| **Wikipedia** | Action API + REST summary | Works |
| **Wikidata** | Action API + Special:EntityData | Works |
| **Wikidata Query Service** | SPARQL | Works, occasional 504 |
| **DuckDuckGo** | Instant Answer API | Works, occasional 202 |
| **Google News** | RSS search | Works, biggest news yield |
| **Bing News** | RSS search | Works |

### Not Currently Usable

- **GDELT**: Rate-limited
- **OpenCorporates**: Requires a token

### Possible Future Sources

- **GLEIF**: Free, no API key
- **OpenSanctions**: Bulk datasets can be downloaded without a key
- **Companies House**: Can be added later

## Current Prototype Stack

**Firecrawl + SEC EDGAR + Wikipedia + Wikidata + Wikidata Query Service + DuckDuckGo + Google News + Bing News**
