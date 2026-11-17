name: Amazon Warehouse 24-7

on:
  schedule:
    - cron: '*/15 * * * *'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 12
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install requests beautifulsoup4 playwright
          playwright install chromium

      - name: Run Amazon Scanner
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
        run: python bot.py

      - name: Save sent deals memory
        run: |
          if [ -f sent_deals.json ]; then
            git config user.name "AmazonBot"
            git config user.email "bot@users.noreply.github.com"
            git add sent_deals.json
            git diff --staged --quiet || git commit -m "Update deals cache"
            git push
          fi
