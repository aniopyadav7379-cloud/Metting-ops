#!/bin/bash

# Quick production build and serve script
# Just builds and serves - no extra checks

echo "🚀 Meeting-Ops Frontend - Production Build"
echo "=========================================="

cd "$(dirname "$0")"

# Build production (skip TypeScript checking for faster builds)
echo "🔨 Building production version..."
npm run build:production

# Use vite preview (already in package.json)
echo "🌐 Starting production preview server..."
npm run preview -- --host 0.0.0.0 --port 7777