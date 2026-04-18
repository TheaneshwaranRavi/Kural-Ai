#!/usr/bin/env bash
set -e

echo "==> Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "==> Upgrading pip..."
pip install --upgrade pip

echo "==> Installing dependencies..."
pip install -r requirements.txt

echo "==> Copying env template..."
[ -f .env ] || cp .env.example .env

echo "==> Creating required directories..."
mkdir -p data vector_db models

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Install Ollama: https://ollama.com"
echo "     ollama pull llama3"
echo "  2. Activate venv: source venv/bin/activate"
echo "  3. Run the agent: python main.py"
