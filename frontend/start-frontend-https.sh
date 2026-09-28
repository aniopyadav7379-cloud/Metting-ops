#!/bin/bash
# Start Meeting-Ops Frontend with HTTPS

echo "🦄 Meeting-Ops Frontend HTTPS Startup"
echo "======================================"

cd "$(dirname "$0")"

# Check if certificates exist
if [ ! -f "localhost-cert.pem" ] || [ ! -f "localhost-key.pem" ]; then
    echo "📜 Generating Magic Unicorn certificates..."
    ./generate-magic-unicorn-cert.sh
    
    if [ $? -ne 0 ]; then
        echo "❌ Failed to generate certificates"
        exit 1
    fi
fi

echo ""
echo "✅ Certificates ready"
echo ""
echo "🚀 Starting HTTPS server on port 7777..."
echo ""
echo "📱 To install the certificate on other devices:"
echo "   1. Run in another terminal: cd frontend && python3 serve-certificate.py"
echo "   2. Visit http://YOUR_IP:8888 from the device"
echo "   3. Download and install the certificate"
echo ""
echo "🌐 Access Meeting-Ops at:"
echo "   https://localhost:7777 (this machine)"
echo "   https://YOUR_IP:7777 (other devices - after installing certificate)"
echo ""
echo "⚠️  First time visitors will see a certificate warning."
echo "   After installing the Magic Unicorn CA certificate, the warning will disappear."
echo ""

# Start with HTTPS
npm run dev:https