#!/usr/bin/env python3
"""
Magic Unicorn Certificate Download Server
Serves the CA certificate for easy installation on client devices
"""
import http.server
import socketserver
import os
from pathlib import Path

PORT = 8888
CERT_DIR = Path("certs")
CA_CERT = CERT_DIR / "magic-unicorn-ca.crt"

class CertificateHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            
            html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>🦄 Magic Unicorn Certificate Authority</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
        }}
        .container {{
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }}
        h1 {{
            color: #764ba2;
            text-align: center;
            font-size: 2em;
        }}
        .emoji {{
            font-size: 3em;
            text-align: center;
            margin: 20px 0;
        }}
        .download-btn {{
            display: block;
            width: 100%;
            padding: 20px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            text-decoration: none;
            border-radius: 10px;
            text-align: center;
            font-size: 1.2em;
            font-weight: bold;
            margin: 20px 0;
            box-shadow: 0 10px 20px rgba(0,0,0,0.2);
            transition: transform 0.2s;
        }}
        .download-btn:hover {{
            transform: scale(1.05);
        }}
        .instructions {{
            background: #f8f9fa;
            border-radius: 10px;
            padding: 20px;
            margin: 20px 0;
        }}
        .instructions h3 {{
            color: #764ba2;
            margin-top: 0;
        }}
        .step {{
            margin: 10px 0;
            padding-left: 20px;
        }}
        .warning {{
            background: #fff3cd;
            border: 1px solid #ffc107;
            border-radius: 10px;
            padding: 15px;
            margin: 20px 0;
        }}
        .company {{
            text-align: center;
            color: #666;
            margin-top: 30px;
            font-size: 0.9em;
        }}
        code {{
            background: #e9ecef;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: monospace;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="emoji">🦄</div>
        <h1>Magic Unicorn Certificate Authority</h1>
        <p style="text-align: center; color: #666;">
            Install our certificate to access Meeting-Ops securely
        </p>
        
        <a href="/download" class="download-btn">
            📥 Download Certificate
        </a>
        
        <div class="warning">
            <strong>⚠️ Security Notice:</strong> This is a self-signed certificate for local/internal use only.
            Only install if you trust Magic Unicorn Inc and this is your Meeting-Ops server.
        </div>
        
        <div class="instructions">
            <h3>📱 Installation Instructions</h3>
            
            <details open>
                <summary><strong>Windows</strong></summary>
                <div class="step">1. Download the certificate using the button above</div>
                <div class="step">2. Double-click the downloaded <code>magic-unicorn-ca.crt</code></div>
                <div class="step">3. Click "Install Certificate"</div>
                <div class="step">4. Select "Local Machine" → Next</div>
                <div class="step">5. Select "Place all certificates in: <strong>Trusted Root Certification Authorities</strong>"</div>
                <div class="step">6. Click Finish and restart your browser</div>
            </details>
            
            <details>
                <summary><strong>macOS</strong></summary>
                <div class="step">1. Download the certificate</div>
                <div class="step">2. Double-click <code>magic-unicorn-ca.crt</code></div>
                <div class="step">3. Add to <strong>System</strong> keychain</div>
                <div class="step">4. Open Keychain Access</div>
                <div class="step">5. Find "Magic Unicorn Root CA"</div>
                <div class="step">6. Double-click → Trust → <strong>Always Trust</strong></div>
            </details>
            
            <details>
                <summary><strong>iPhone/iPad</strong></summary>
                <div class="step">1. Download the certificate on your device</div>
                <div class="step">2. Go to Settings → Profile Downloaded</div>
                <div class="step">3. Tap Install (enter passcode if needed)</div>
                <div class="step">4. Settings → General → About → Certificate Trust Settings</div>
                <div class="step">5. Enable for "Magic Unicorn Root CA"</div>
            </details>
            
            <details>
                <summary><strong>Android</strong></summary>
                <div class="step">1. Download the certificate</div>
                <div class="step">2. Settings → Security → Install from storage</div>
                <div class="step">3. Select the certificate file</div>
                <div class="step">4. Name it "Magic Unicorn CA"</div>
            </details>
        </div>
        
        <div class="instructions">
            <h3>🚀 After Installation</h3>
            <p>Once installed, you can access Meeting-Ops at:</p>
            <p style="text-align: center; font-size: 1.2em;">
                <code>https://{self.headers['Host'].split(':')[0]}:7777</code>
            </p>
        </div>
        
        <div class="company">
            <p>🦄 Magic Unicorn Unconventional Technology & Stuff Inc.</p>
            <p>"Making the impossible, possible!"</p>
        </div>
    </div>
</body>
</html>
"""
            self.wfile.write(html.encode())
            
        elif self.path == "/download":
            if CA_CERT.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/x-x509-ca-cert")
                self.send_header("Content-Disposition", 'attachment; filename="magic-unicorn-ca.crt"')
                self.end_headers()
                
                with open(CA_CERT, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "Certificate not found")
        else:
            self.send_error(404)

if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    
    if not CA_CERT.exists():
        print("❌ Certificate not found! Run generate-magic-unicorn-cert.sh first")
        exit(1)
    
    with socketserver.TCPServer(("0.0.0.0", PORT), CertificateHandler) as httpd:
        print(f"🦄 Magic Unicorn Certificate Server")
        print(f"====================================")
        print(f"📥 Certificate download page: http://0.0.0.0:{PORT}")
        print(f"")
        print(f"Share this link with devices that need the certificate:")
        
        # Get local IP
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        print(f"   http://{local_ip}:{PORT}")
        print(f"")
        print(f"Press Ctrl+C to stop")
        print(f"")
        
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n✨ Server stopped")