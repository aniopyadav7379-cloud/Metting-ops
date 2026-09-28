#!/bin/bash
# Setup HTTPS for local development to enable microphone access

echo "🔐 Setting up HTTPS for Meeting-Ops Frontend"
echo "==========================================="

cd "$(dirname "$0")"

# Check if certificates already exist
if [ -f "localhost-cert.pem" ] && [ -f "localhost-key.pem" ]; then
    echo "✅ Certificates already exist"
    echo ""
    echo "To regenerate, delete the existing files first:"
    echo "  rm localhost-cert.pem localhost-key.pem"
    exit 0
fi

echo "📝 Generating self-signed certificate..."
echo ""

# Generate self-signed certificate
openssl req -x509 -newkey rsa:2048 \
    -keyout localhost-key.pem \
    -out localhost-cert.pem \
    -days 365 \
    -nodes \
    -subj "/C=US/ST=State/L=City/O=MeetingOps/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.0/24"

if [ $? -eq 0 ]; then
    echo "✅ Certificates generated successfully!"
    echo ""
    echo "📁 Files created:"
    echo "   - localhost-cert.pem (certificate)"
    echo "   - localhost-key.pem (private key)"
    echo ""
    echo "🚀 To start the frontend with HTTPS:"
    echo "   npm run dev:https"
    echo ""
    echo "⚠️  Your browser will show a security warning."
    echo "   This is normal for self-signed certificates."
    echo "   Click 'Advanced' and 'Proceed to localhost' to continue."
else
    echo "❌ Failed to generate certificates"
    echo "   Make sure OpenSSL is installed: sudo apt install openssl"
    exit 1
fi