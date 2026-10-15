### AI

..

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app ai_erpnext
```

### AI provider configuration

Open **AI Provider Settings** (System Manager only). Keys are stored encrypted in Password fields and are never shown again, logged or sent to the browser.

- **Primary AI Provider** / **Primary API Key**: Groq, OpenAI, OpenRouter and Nvidia NIM work for chat and invoice images; Gemini for invoice images only. Leave **Base URL** and **Model Name** empty to use the provider defaults (OpenRouter and Nvidia NIM need a model name).
- **Enable Fallback Provider** (off by default): when Groq reports that its *daily* quota is exhausted, one request goes to the fallback (OpenAI, e.g. `gpt-4.1-mini`). Per-minute limits, outages, invalid keys, missing credits and other errors never switch providers, and a failing fallback is never retried. **Fallback Daily Request Limit** caps paid requests per site per day.
- The fallback is a paid service: enable it only after its account has credits. Saving the settings checks them locally and never calls a provider.

Changing the provider or a key takes effect immediately; no code change or deployment is needed. After updating the app, run `bench --site <site> migrate` to apply the settings layout.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ai_erpnext
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
