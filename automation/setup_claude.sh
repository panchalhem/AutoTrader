#!/bin/bash

# setup_claude.sh - Script to install and configure Claude CLI

# 1. Check for Node.js
if ! command -v node &> /dev/null
then
    echo "Error: Node.js is not installed. Please install it first (v18+)."
    exit 1
fi

# 2. Install Claude Code globally
# Note: Using sudo if necessary for global installation
echo "Installing @anthropic-ai/claude-code globally..."
sudo npm install -g @anthropic-ai/claude-code

# 3. Verify installation
if command -v claude &> /dev/null
then
    echo "Claude CLI installed successfully!"
else
    echo "Installation failed. Check for npm errors above."
    exit 1
fi

echo ""
echo "Configuration Instructions:"
echo "---------------------------"
echo "The official Claude CLI requires an Anthropic API Key."
echo "1. Get your key at: https://console.anthropic.com/"
echo "2. Add it to your shell profile to avoid re-entering it:"
echo "   echo 'export ANTHROPIC_API_KEY=\"your_key_here\"' >> ~/.bashrc"
echo "   source ~/.bashrc"
echo ""
echo "Once configured, you can start Claude by typing: claude"
