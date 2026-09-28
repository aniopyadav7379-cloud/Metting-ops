#!/bin/bash

# Quick start without TypeScript checking
# Use this when you want to test functionality despite type errors

echo "⚡ Meeting-Ops Frontend - Quick Start (No TypeScript)"
echo "=================================================="

cd "$(dirname "$0")"

# Build without TypeScript checking
echo "🔨 Building (skipping TypeScript)..."
npx vite build --mode production

if [ $? -ne 0 ]; then
    echo "❌ Build failed!"
    exit 1
fi

echo "✅ Build successful!"
echo "🌐 Starting server on port 7777..."

# Start production server
npx vite preview --host 0.0.0.0 --port 7777