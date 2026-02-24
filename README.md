# EP Syncs

> Sync scripts connecting Protect the Vote (PTV) shift scheduling and Airtable to BigQuery for election protection volunteer data.

## Setup

```bash
# Install the shared connections library (do this once per machine)
pip install -e "C:/Users/RobKerth/OneDrive - Common Cause Education Fund/Documents/AI Interpretation/ccef-connections"

pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

## Usage

_TODO_

## Project Structure

```
ep-syncs/
├── .claude/         # Claude Code configuration
├── .env.example     # Credential template (copy to .env, never commit .env)
├── README.md
└── requirements.txt
```
