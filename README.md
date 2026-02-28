# UFC Betting Analyzer

A comprehensive UFC sports betting analysis system with a beautiful dark-themed web dashboard. Scrapes fighter stats, builds ELO ratings, trains a logistic regression model, compares model-implied odds to market odds, and surfaces +EV edges with Kelly Criterion position sizing.

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Flask](https://img.shields.io/badge/Flask-3.0+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

- **Data Collection** — Scrapers for UFCStats.com, Sherdog.com, and the-odds-api.com with caching and rate limiting
- **ELO Rating System** — Separate striking, grappling, and cardio ratings with method-specific K-factor bonuses
- **Prediction Model** — Logistic regression with 19 features (ELO diffs, style matchups, physical attributes, activity factors)
- **Edge Detection** — Flags bets where model disagrees with market by >5% implied probability
- **Kelly Criterion** — 25% fractional Kelly position sizing with confidence tiers
- **Prop Analysis** — Method of victory probabilities and over/under round totals
- **Web Dashboard** — Beautiful dark-themed UI with fight cards, fighter profiles, edge rankings, and backtest results
- **CLI Pipeline** — Full data pipeline runnable with a single command

---

## Installation on macOS

### Prerequisites

- **Python 3.9+** (macOS comes with Python, but you may want a newer version)
- **pip** (Python package manager)
- A free API key from [the-odds-api.com](https://the-odds-api.com/) (optional, for live odds)

### Step 1: Install Python (if needed)

If you don't have Python 3.9+ installed, use Homebrew:

```bash
# Install Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.11
```

### Step 2: Clone the Repository

```bash
git clone https://github.com/powoso/Claude-ufcanalysis.git
cd Claude-ufcanalysis
```

### Step 3: Create a Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 4: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 5: Set Your Odds API Key (Optional)

Get a free API key at [the-odds-api.com](https://the-odds-api.com/), then:

```bash
export ODDS_API_KEY="your_api_key_here"
```

To make it permanent, add the line above to your `~/.zshrc` (or `~/.bash_profile`):

```bash
echo 'export ODDS_API_KEY="your_api_key_here"' >> ~/.zshrc
source ~/.zshrc
```

### Step 6: Run the Data Pipeline

Scrape fight data, train the model, and analyze upcoming cards:

```bash
# Full pipeline (scrape → train → analyze)
python main.py

# Or quick mode for faster first run
python main.py --quick
```

### Step 7: Launch the Web Dashboard

```bash
python webapp.py
```

Open your browser to **http://localhost:5000**

---

## Usage

### Web Dashboard

```bash
python webapp.py
```

Then visit http://localhost:5000. The dashboard includes:

| Page | Description |
|------|-------------|
| **Dashboard** | System overview, upcoming & recent events |
| **Edge Finder** | Ranked +EV bets with Kelly sizing |
| **Fighters** | Browse all fighters sorted by ELO, with search |
| **Fighter Profile** | Detailed stats, ELO breakdown, fight history |
| **Event Detail** | Full fight card with predictions, odds, and edges |
| **Backtest** | Model accuracy over historical fights |

### CLI Commands

```bash
python main.py                  # Full pipeline: scrape, train, analyze
python main.py --scrape         # Scrape data only
python main.py --train          # Train/retrain the prediction model
python main.py --analyze        # Analyze upcoming card (uses cached data)
python main.py --backtest       # Backtest model against history
python main.py --report         # Generate markdown report
python main.py --quick          # Quick mode: smaller scrape, faster run
python main.py -v               # Verbose logging
```

### Running Tests

```bash
python -m unittest tests.test_core -v
```

---

## Project Structure

```
├── main.py                     # CLI entry point
├── webapp.py                   # Flask web application
├── config.py                   # All configuration constants
├── database.py                 # SQLite schema (10 tables) + query helpers
├── requirements.txt
├── web/
│   ├── templates/              # Jinja2 HTML templates
│   │   ├── base.html           # Layout with sidebar navigation
│   │   ├── dashboard.html      # Main dashboard
│   │   ├── event.html          # Fight card with predictions
│   │   ├── edges.html          # Edge finder with Kelly sizing
│   │   ├── fighter.html        # Fighter profile page
│   │   ├── fighters.html       # Fighter list with search
│   │   └── backtest.html       # Backtest results
│   └── static/
│       ├── css/style.css       # Dark theme stylesheet
│       └── js/app.js
├── scrapers/
│   ├── cache.py                # File-based HTTP response cache
│   ├── ufcstats.py             # UFCStats.com scraper
│   ├── sherdog.py              # Sherdog.com scraper
│   ├── odds_api.py             # the-odds-api.com client
│   └── ingest.py               # Data ingestion orchestrator
├── analysis/
│   ├── fighter_profile.py      # Weighted stats, style classification
│   ├── edge_detection.py       # Kelly Criterion, edge finding
│   └── prop_bets.py            # Method of victory, over/under
├── models/
│   ├── elo.py                  # MMA-adapted ELO system
│   └── predictor.py            # Logistic regression model
├── reports/
│   └── generator.py            # Markdown report generator
└── tests/
    └── test_core.py            # 33 unit tests
```

---

## How It Works

### ELO Rating System

Each fighter has four separate ELO ratings: **Overall**, **Striking**, **Grappling**, and **Cardio**. When a fight is resolved:

- **KO/TKO** wins boost the winner's striking ELO by 1.5x and reduce grappling impact to 0.3x
- **Submissions** boost grappling ELO by 1.5x
- **Decisions** boost cardio ELO by 1.4x (rewarding the ability to go the distance)
- First-round finishes get a 1.3x K-factor bonus

### Prediction Model

A logistic regression model with 19 features:

- ELO differentials (overall, striking, grappling, cardio)
- Striking stats (sig. strikes landed, accuracy, knockdowns)
- Grappling stats (takedowns, accuracy, submission attempts, control time)
- Win rate and finish rate differentials
- Physical attributes (reach, height)
- Style and stance matchup modifiers
- Age and inactivity factors
- Experience differential

The final prediction blends 60% logistic regression + 40% ELO baseline.

### Edge Detection

1. Model predicts win probability for each fighter
2. Compares to market-implied probability from sportsbook odds
3. Flags fights where the model disagrees by >5%
4. Calculates 25% fractional Kelly position sizing
5. Classifies confidence: **HIGH** (>15% edge), **MEDIUM** (>8%), **LOW** (>5%)

---

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `KELLY_FRACTION` | 0.25 | Fraction of full Kelly (25%) |
| `EDGE_THRESHOLD` | 0.05 | Minimum edge to flag (5%) |
| `BANKROLL` | 1000 | Default bankroll in units |
| `ELO_K_FACTOR` | 32 | ELO update magnitude |
| `RECENCY_DECAY` | 0.85 | Weight decay per fight back |
| `CACHE_TTL_HOURS` | 6 | Hours before re-fetching cached data |
| `RATE_LIMIT_DELAY` | 2.0 | Seconds between scrape requests |

---

## Troubleshooting

**"No upcoming events"** — Run `python main.py --scrape` to fetch event data.

**"No trained model"** — Run `python main.py --train` after scraping data.

**"No odds data"** — Set `ODDS_API_KEY` environment variable with your free key from the-odds-api.com.

**Port already in use** — Change the port: `PORT=8080 python webapp.py`

---

## Disclaimer

This tool is for informational and entertainment purposes only. Past model performance does not guarantee future results. Always gamble responsibly.
