#!/bin/bash
# Generate SSL certificates signed by Magic Unicorn Inc.
# This creates a CA certificate that can be installed on devices for trust

set -e

CERT_DIR="./certs"
CA_KEY="$CERT_DIR/magic-unicorn-ca.key"
CA_CERT="$CERT_DIR/magic-unicorn-ca.crt"
SERVER_KEY="$CERT_DIR/meeting-ops.key"
SERVER_CSR="$CERT_DIR/meeting-ops.csr"
SERVER_CERT="$CERT_DIR/meeting-ops.crt"
SERVER_PEM="$CERT_DIR/meeting-ops.pem"

echo "🦄 Magic Unicorn Unconventional Technology & Stuff Inc."
echo "========================================================="
echo "SSL Certificate Generation System"
echo ""

# Create certs directory
mkdir -p "$CERT_DIR"

# Step 1: Generate CA private key
if [ ! -f "$CA_KEY" ]; then
    echo "🔐 Generating Magic Unicorn CA private key..."
    openssl genrsa -out "$CA_KEY" 4096
else
    echo "✅ CA private key already exists"
fi

# Step 2: Generate CA certificate
if [ ! -f "$CA_CERT" ]; then
    echo "📜 Creating Magic Unicorn Root Certificate Authority..."
    openssl req -new -x509 -days 3650 -key "$CA_KEY" -out "$CA_CERT" \
        -subj "/C=US/ST=California/L=Silicon Valley/O=Magic Unicorn Unconventional Technology & Stuff Inc/OU=Unicorn Security Division/CN=Magic Unicorn Root CA/emailAddress=security@magicunicorn.tech"
    
    echo ""
    echo "✨ Magic Unicorn CA Certificate created!"
    echo "   Organization: Magic Unicorn Unconventional Technology & Stuff Inc"
    echo "   Valid for: 10 years"
else
    echo "✅ CA certificate already exists"
fi

# Step 3: Generate server private key
echo ""
echo "🔑 Generating Meeting-Ops server private key..."
openssl genrsa -out "$SERVER_KEY" 2048

# Step 4: Create certificate signing request with SAN
echo "📝 Creating certificate signing request..."
cat > "$CERT_DIR/meeting-ops.conf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
C = US
ST = California
L = Silicon Valley
O = Magic Unicorn Inc
OU = Meeting-Ops Division
CN = meeting-ops.local
emailAddress = admin@magicunicorn.tech

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = meeting-ops.local
DNS.3 = *.meeting-ops.local
DNS.4 = meetingops.local
DNS.5 = uc-1.local
IP.1 = 127.0.0.1
IP.2 = ::1
IP.3 = 192.168.1.145
IP.4 = 192.168.1.223
IP.5 = 192.168.0.1
IP.6 = 10.0.0.1
IP.7 = 172.16.0.1
EOF

openssl req -new -key "$SERVER_KEY" -out "$SERVER_CSR" -config "$CERT_DIR/meeting-ops.conf"

# Step 5: Sign the server certificate with our CA
echo "✍️  Signing certificate with Magic Unicorn CA..."
cat > "$CERT_DIR/v3.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = meeting-ops.local
DNS.3 = *.meeting-ops.local
DNS.4 = meetingops.local
DNS.5 = uc-1.local
IP.1 = 127.0.0.1
IP.2 = ::1
EOF

# Add all local network IPs
for i in {1..254}; do
    echo "IP.$((i+2)) = 192.168.1.$i" >> "$CERT_DIR/v3.ext"
done

openssl x509 -req -in "$SERVER_CSR" \
    -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$SERVER_CERT" -days 365 \
    -extfile "$CERT_DIR/v3.ext"

# Step 6: Create PEM file for Vite
echo "📦 Creating PEM bundle for Vite..."
cat "$SERVER_CERT" "$SERVER_KEY" > "$SERVER_PEM"

# Create symbolic links for Vite
ln -sf "$CERT_DIR/meeting-ops.crt" localhost-cert.pem
ln -sf "$CERT_DIR/meeting-ops.key" localhost-key.pem

echo ""
echo "🎉 SUCCESS! Certificates generated!"
echo "===================================="
echo ""
echo "📁 Certificate Files:"
echo "   CA Certificate:     $CA_CERT"
echo "   Server Certificate: $SERVER_CERT"
echo "   Server Key:        $SERVER_KEY"
echo ""
echo "🌐 Certificate Details:"
openssl x509 -in "$SERVER_CERT" -noout -subject -issuer -dates | sed 's/^/   /'
echo ""
echo "📱 To install the CA certificate on client devices:"
echo ""
echo "   🖥️  Windows:"
echo "      1. Double-click: $CA_CERT"
echo "      2. Click 'Install Certificate'"
echo "      3. Select 'Local Machine' → Next"
echo "      4. Select 'Place all certificates in: Trusted Root Certification Authorities'"
echo "      5. Finish and restart browser"
echo ""
echo "   🍎 macOS:"
echo "      1. Double-click: $CA_CERT"
echo "      2. Add to System keychain"
echo "      3. Open Keychain Access"
echo "      4. Find 'Magic Unicorn Root CA'"
echo "      5. Double-click → Trust → Always Trust"
echo ""
echo "   🐧 Linux:"
echo "      sudo cp $CA_CERT /usr/local/share/ca-certificates/"
echo "      sudo update-ca-certificates"
echo ""
echo "   📱 Android:"
echo "      1. Copy $CA_CERT to device"
echo "      2. Settings → Security → Install from storage"
echo "      3. Select the certificate file"
echo ""
echo "   📱 iOS:"
echo "      1. Email or AirDrop $CA_CERT to device"
echo "      2. Open the file → Install Profile"
echo "      3. Settings → General → About → Certificate Trust Settings"
echo "      4. Enable for 'Magic Unicorn Root CA'"
echo ""
echo "🚀 To start the server with HTTPS:"
echo "   npm run dev:https"
echo ""
echo "🦄 Magic Unicorn Inc - Making the impossible, possible!"