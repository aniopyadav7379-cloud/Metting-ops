#!/bin/bash
# Authentik Setup and Configuration Script for Meeting-Ops
# Configures OAuth providers, LDAP, and application integration

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
NC='\033[0m'

# Configuration
DOMAIN=${DOMAIN:-meeting-ops.local}
AUTHENTIK_URL="https://auth.${DOMAIN}"
API_URL="https://api.${DOMAIN}"
FRONTEND_URL="https://${DOMAIN}"

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[✓]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[!]${NC} $1"; }
print_error() { echo -e "${RED}[✗]${NC} $1"; }
print_step() { echo -e "${PURPLE}[STEP]${NC} $1"; }

# Banner
echo -e "${PURPLE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              Authentik SSO Setup for Meeting-Ops            ║"
echo "║          Office 365 • Google Workspace • LDAP/AD           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_warning ".env file not found. Creating from template..."
    cp .env.sso .env
    print_warning "Please edit .env file with your configuration and run this script again"
    exit 1
fi

# Source environment
set -a
source .env
set +a

print_step "Starting Authentik configuration for domain: $DOMAIN"

# Wait for Authentik to be ready
print_info "Waiting for Authentik to start..."
timeout=300
count=0

while [ $count -lt $timeout ]; do
    if curl -sf "$AUTHENTIK_URL/if/flow/initial-setup/" > /dev/null 2>&1; then
        print_success "Authentik is ready!"
        break
    fi
    
    if [ $((count % 30)) -eq 0 ]; then
        print_info "Still waiting for Authentik... ($count/$timeout seconds)"
    fi
    
    sleep 5
    count=$((count + 5))
done

if [ $count -ge $timeout ]; then
    print_error "Authentik failed to start within $timeout seconds"
    print_info "Check logs: docker logs authentik-server"
    exit 1
fi

# Create Authentik configuration script
print_step "Creating Authentik configuration..."

cat > authentik/bootstrap.py << 'EOF'
#!/usr/bin/env python3
"""
Authentik Bootstrap Configuration for Meeting-Ops
Configures applications, flows, and providers automatically
"""
import os
import requests
import json
import time
from urllib.parse import urljoin

class AuthentikBootstrap:
    def __init__(self):
        self.base_url = os.getenv('AUTHENTIK_URL', 'http://authentik-server:9000')
        self.api_token = os.getenv('AUTHENTIK_API_TOKEN', '')
        self.domain = os.getenv('DOMAIN', 'meeting-ops.local')
        
        self.headers = {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json'
        } if self.api_token else {'Content-Type': 'application/json'}
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
    
    def wait_for_authentik(self):
        """Wait for Authentik to be ready"""
        print("🔄 Waiting for Authentik to be ready...")
        max_attempts = 60
        
        for attempt in range(max_attempts):
            try:
                response = self.session.get(f"{self.base_url}/api/v3/core/applications/")
                if response.status_code == 200:
                    print("✅ Authentik is ready!")
                    return True
                elif response.status_code == 403:
                    print("⚠️ API token required. Please set AUTHENTIK_API_TOKEN environment variable")
                    return False
            except requests.exceptions.ConnectionError:
                pass
            
            if attempt % 10 == 0:
                print(f"⏳ Still waiting... ({attempt}/{max_attempts})")
            
            time.sleep(5)
        
        print("❌ Authentik failed to start")
        return False
    
    def create_application(self, name, slug, client_id=None):
        """Create OAuth2 application in Authentik"""
        print(f"🔧 Creating application: {name}")
        
        # Check if application already exists
        response = self.session.get(f"{self.base_url}/api/v3/core/applications/")
        if response.status_code == 200:
            for app in response.json().get('results', []):
                if app['slug'] == slug:
                    print(f"✅ Application {name} already exists")
                    return app
        
        # Create OAuth2 provider first
        provider_data = {
            "name": f"{name} Provider",
            "client_type": "confidential",
            "client_id": client_id or f"{slug}-client",
            "authorization_grant_type": "authorization-code",
            "redirect_uris": f"https://{self.domain}/auth/callback\nhttps://api.{self.domain}/auth/callback",
            "post_logout_redirect_uris": f"https://{self.domain}/",
            "sub_mode": "hashed_user_id",
            "access_token_validity": "minutes=10",
            "refresh_token_validity": "days=30"
        }
        
        response = self.session.post(
            f"{self.base_url}/api/v3/providers/oauth2/",
            json=provider_data
        )
        
        if response.status_code not in [200, 201]:
            print(f"❌ Failed to create OAuth2 provider: {response.text}")
            return None
        
        provider = response.json()
        print(f"✅ Created OAuth2 provider: {provider['name']}")
        
        # Create application
        app_data = {
            "name": name,
            "slug": slug,
            "provider": provider['pk'],
            "meta_launch_url": f"https://{self.domain}",
            "meta_description": f"{name} - Meeting transcription and AI analysis"
        }
        
        response = self.session.post(
            f"{self.base_url}/api/v3/core/applications/",
            json=app_data
        )
        
        if response.status_code not in [200, 201]:
            print(f"❌ Failed to create application: {response.text}")
            return None
        
        application = response.json()
        print(f"✅ Created application: {name}")
        return application
    
    def setup_oauth_sources(self):
        """Setup OAuth2 sources for Office 365, Google, etc."""
        sources = []
        
        # Office 365 / Azure AD
        if os.getenv('AZURE_CLIENT_ID') and os.getenv('AZURE_CLIENT_SECRET'):
            print("🔧 Setting up Office 365 / Azure AD source...")
            
            azure_data = {
                "name": "Office 365",
                "slug": "office365",
                "provider_type": "microsoft",
                "consumer_key": os.getenv('AZURE_CLIENT_ID'),
                "consumer_secret": os.getenv('AZURE_CLIENT_SECRET'),
                "additional_parameters": {
                    "tenant": os.getenv('AZURE_TENANT_ID', 'common')
                }
            }
            
            response = self.session.post(
                f"{self.base_url}/api/v3/sources/oauth/",
                json=azure_data
            )
            
            if response.status_code in [200, 201]:
                sources.append('Office 365')
                print("✅ Office 365 source configured")
            else:
                print(f"❌ Failed to create Office 365 source: {response.text}")
        
        # Google Workspace
        if os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET'):
            print("🔧 Setting up Google Workspace source...")
            
            google_data = {
                "name": "Google Workspace",
                "slug": "google",
                "provider_type": "google",
                "consumer_key": os.getenv('GOOGLE_CLIENT_ID'),
                "consumer_secret": os.getenv('GOOGLE_CLIENT_SECRET')
            }
            
            response = self.session.post(
                f"{self.base_url}/api/v3/sources/oauth/",
                json=google_data
            )
            
            if response.status_code in [200, 201]:
                sources.append('Google Workspace')
                print("✅ Google Workspace source configured")
            else:
                print(f"❌ Failed to create Google source: {response.text}")
        
        return sources
    
    def setup_ldap_source(self):
        """Setup LDAP/AD source"""
        if not os.getenv('LDAP_SERVER_URI'):
            print("⏭️ No LDAP configuration found, skipping...")
            return None
        
        print("🔧 Setting up LDAP/Active Directory source...")
        
        ldap_data = {
            "name": "Active Directory",
            "slug": "active-directory",
            "server_uri": os.getenv('LDAP_SERVER_URI'),
            "bind_cn": os.getenv('LDAP_BIND_DN'),
            "bind_password": os.getenv('LDAP_BIND_PASSWORD'),
            "base_dn": os.getenv('LDAP_BASE_DN'),
            "additional_user_dn": os.getenv('LDAP_USER_DN', ''),
            "additional_group_dn": os.getenv('LDAP_GROUP_DN', ''),
            "user_object_filter": "(objectClass=person)",
            "group_object_filter": "(objectClass=group)",
            "sync_users": True,
            "sync_groups": True
        }
        
        response = self.session.post(
            f"{self.base_url}/api/v3/sources/ldap/",
            json=ldap_data
        )
        
        if response.status_code in [200, 201]:
            print("✅ LDAP source configured")
            return "Active Directory"
        else:
            print(f"❌ Failed to create LDAP source: {response.text}")
            return None
    
    def run_bootstrap(self):
        """Run complete bootstrap process"""
        print("🚀 Starting Authentik bootstrap for Meeting-Ops...")
        
        if not self.wait_for_authentik():
            return False
        
        # Create main Meeting-Ops application
        app = self.create_application("Meeting-Ops", "meeting-ops")
        if not app:
            print("❌ Failed to create main application")
            return False
        
        # Setup OAuth sources
        oauth_sources = self.setup_oauth_sources()
        
        # Setup LDAP source
        ldap_source = self.setup_ldap_source()
        
        print("\n" + "="*60)
        print("✅ Authentik bootstrap completed!")
        print(f"🌐 Authentik URL: https://auth.{self.domain}")
        print(f"🚀 Meeting-Ops URL: https://{self.domain}")
        
        if oauth_sources:
            print(f"🔗 OAuth Sources: {', '.join(oauth_sources)}")
        if ldap_source:
            print(f"📁 LDAP Source: {ldap_source}")
        
        print("\n📋 Next steps:")
        print("1. Access Authentik admin interface")
        print("2. Complete initial setup wizard")
        print("3. Configure outpost for Traefik")
        print("4. Test login flows")
        print("="*60)
        
        return True

if __name__ == "__main__":
    bootstrap = AuthentikBootstrap()
    success = bootstrap.run_bootstrap()
    exit(0 if success else 1)
EOF

chmod +x authentik/bootstrap.py

print_success "Authentik bootstrap script created"

# Create outpost configuration
print_step "Creating outpost configuration..."

cat > authentik/outpost-config.yml << EOF
# Authentik Outpost Configuration for Traefik
authentik_host: https://auth.${DOMAIN}
authentik_host_insecure: false
authentik_host_browser: https://auth.${DOMAIN}

object_naming_template: meeting-ops

log_level: info
error_reporting:
  enabled: false

# Traefik integration
providers:
  traefik:
    name: meeting-ops-traefik
    external_host: https://${DOMAIN}
    
# Forward auth configuration    
forward_auth:
  name: meeting-ops-forward-auth
  external_host: https://${DOMAIN}
  internal_host: http://authentik-server:9000
  internal_host_ssl_validation: false
EOF

print_success "Outpost configuration created"

print_step "Creating startup script..."

cat > start-sso.sh << 'EOF'
#!/bin/bash
# Start Meeting-Ops with SSO (Authentik + Traefik)

set -e

echo "🚀 Starting Meeting-Ops with SSO..."

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found. Copy .env.sso to .env and configure it first."
    exit 1
fi

# Generate secure secret if not set
if grep -q "please-change-this" .env; then
    echo "🔐 Generating secure Authentik secret key..."
    SECRET=$(openssl rand -base64 32)
    sed -i "s/please-change-this-to-a-secure-random-key/$SECRET/g" .env
    echo "✅ Secret key generated"
fi

# Create necessary directories
mkdir -p authentik/{media,templates,certs}
mkdir -p traefik
mkdir -p postgres/init

# Start services
echo "🐳 Starting Docker services..."
docker compose -f docker-compose-sso.yml up -d

echo "⏳ Waiting for services to start..."
sleep 30

echo "🔧 Running Authentik bootstrap..."
docker compose -f docker-compose-sso.yml exec -T authentik-server python3 /bootstrap.py

echo ""
echo "✅ Meeting-Ops SSO is starting up!"
echo ""
echo "🌐 URLs:"
echo "  - Frontend: https://${DOMAIN:-meeting-ops.local}"
echo "  - API: https://api.${DOMAIN:-meeting-ops.local}"
echo "  - Auth: https://auth.${DOMAIN:-meeting-ops.local}"
echo "  - Traefik: https://traefik.${DOMAIN:-meeting-ops.local}:8080"
echo ""
echo "📋 Next steps:"
echo "1. Add DNS entries or update /etc/hosts"
echo "2. Complete Authentik setup wizard"
echo "3. Configure OAuth providers in Authentik admin"
echo "4. Test authentication flows"
echo ""
EOF

chmod +x start-sso.sh

print_success "Startup script created"

print_step "Creating DNS helper script..."

cat > setup-dns.sh << EOF
#!/bin/bash
# Setup DNS entries for Meeting-Ops SSO

DOMAIN=\${DOMAIN:-meeting-ops.local}
IP=\${HOST_IP:-127.0.0.1}

echo "🌐 Setting up DNS entries for \$DOMAIN"

# Add to /etc/hosts (requires sudo)
echo "# Meeting-Ops SSO Entries" | sudo tee -a /etc/hosts
echo "\$IP \$DOMAIN" | sudo tee -a /etc/hosts
echo "\$IP api.\$DOMAIN" | sudo tee -a /etc/hosts  
echo "\$IP auth.\$DOMAIN" | sudo tee -a /etc/hosts
echo "\$IP traefik.\$DOMAIN" | sudo tee -a /etc/hosts
echo "\$IP qdrant.\$DOMAIN" | sudo tee -a /etc/hosts
echo "\$IP ollama.\$DOMAIN" | sudo tee -a /etc/hosts

echo "✅ DNS entries added to /etc/hosts"
echo ""
echo "For production, add these DNS A records:"
echo "  \$DOMAIN -> \$IP"
echo "  *.\$DOMAIN -> \$IP"
EOF

chmod +x setup-dns.sh

print_success "DNS setup script created"

print_info "Creating OAuth provider setup guides..."

mkdir -p docs/oauth-setup

# Office 365 setup guide
cat > docs/oauth-setup/office365.md << 'EOF'
# Office 365 / Azure AD Setup for Meeting-Ops

## 1. Register Application in Azure Portal

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Azure Active Directory** > **App registrations** > **New registration**
3. Fill in details:
   - **Name**: Meeting-Ops
   - **Supported account types**: Accounts in this organizational directory only
   - **Redirect URI**: `https://auth.meeting-ops.local/source/oauth/callback/office365/`

## 2. Configure Application

1. Note the **Application (client) ID** - this is your `AZURE_CLIENT_ID`
2. Note the **Directory (tenant) ID** - this is your `AZURE_TENANT_ID`
3. Go to **Certificates & secrets** > **New client secret**
4. Copy the secret value - this is your `AZURE_CLIENT_SECRET`

## 3. Set Permissions

1. Go to **API permissions**
2. Add permissions:
   - Microsoft Graph > Delegated > `User.Read`
   - Microsoft Graph > Delegated > `email`
   - Microsoft Graph > Delegated > `openid`
   - Microsoft Graph > Delegated > `profile`

## 4. Update .env file

```bash
AZURE_CLIENT_ID=your-application-id
AZURE_CLIENT_SECRET=your-client-secret
AZURE_TENANT_ID=your-tenant-id
```

## 5. Test Login

1. Restart services: `./start-sso.sh`
2. Go to `https://auth.meeting-ops.local`
3. You should see Office 365 as a login option
EOF

# Google Workspace setup guide
cat > docs/oauth-setup/google-workspace.md << 'EOF'
# Google Workspace Setup for Meeting-Ops

## 1. Create Project in Google Cloud Console

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create new project or select existing one
3. Enable **Google+ API** and **Google Identity API**

## 2. Configure OAuth Consent Screen

1. Go to **APIs & Services** > **OAuth consent screen**
2. Choose **Internal** (for workspace) or **External**
3. Fill in required details:
   - App name: Meeting-Ops
   - User support email: your-admin@company.com
   - Developer contact: your-admin@company.com

## 3. Create OAuth2 Credentials

1. Go to **APIs & Services** > **Credentials**
2. Click **Create Credentials** > **OAuth 2.0 Client IDs**
3. Choose **Web application**
4. Add redirect URI: `https://auth.meeting-ops.local/source/oauth/callback/google/`

## 4. Configure Scopes

Add these scopes in OAuth consent screen:
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/userinfo.profile`
- `openid`

## 5. Update .env file

```bash
GOOGLE_CLIENT_ID=your-client-id.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

## 6. Test Login

1. Restart services: `./start-sso.sh`
2. Go to `https://auth.meeting-ops.local`
3. You should see Google as a login option
EOF

# LDAP/AD setup guide
cat > docs/oauth-setup/active-directory.md << 'EOF'
# Active Directory / LDAP Setup for Meeting-Ops

## 1. Create Service Account

Create a dedicated service account in Active Directory:
- Username: `meeting-ops-service`
- Password: Strong password
- Permissions: Read access to users and groups

## 2. Configure LDAP Settings

Update .env file with your AD configuration:

```bash
# Active Directory LDAP Configuration
LDAP_SERVER_URI=ldap://your-domain-controller.local:389
LDAP_BIND_DN=CN=meeting-ops-service,OU=Service Accounts,DC=company,DC=local
LDAP_BIND_PASSWORD=ServiceAccountPassword
LDAP_BASE_DN=DC=company,DC=local
LDAP_USER_DN=OU=Users,DC=company,DC=local
LDAP_GROUP_DN=OU=Groups,DC=company,DC=local
```

## 3. LDAPS (Secure LDAP)

For production, use LDAPS:
```bash
LDAP_SERVER_URI=ldaps://your-domain-controller.local:636
```

## 4. Test LDAP Connection

You can test the LDAP connection using ldapsearch:
```bash
ldapsearch -H ldap://your-domain-controller.local:389 \
  -D "CN=meeting-ops-service,OU=Service Accounts,DC=company,DC=local" \
  -w "ServiceAccountPassword" \
  -b "OU=Users,DC=company,DC=local" \
  "(objectClass=person)"
```

## 5. Group Mapping

In Authentik admin, you can map AD groups to Meeting-Ops roles:
- `CN=Meeting-Ops-Admins,OU=Groups,DC=company,DC=local` → Admin
- `CN=Meeting-Ops-Users,OU=Groups,DC=company,DC=local` → User
EOF

print_success "OAuth provider setup guides created in docs/oauth-setup/"

echo ""
echo -e "${GREEN}✅ Authentik SSO setup complete!${NC}"
echo ""
echo -e "${BLUE}📋 Next steps:${NC}"
echo "1. Review and edit .env file with your configuration"
echo "2. Run ./setup-dns.sh to configure local DNS"
echo "3. Run ./start-sso.sh to start the SSO stack"
echo "4. Follow the OAuth setup guides in docs/oauth-setup/"
echo ""
echo -e "${BLUE}🌐 URLs after startup:${NC}"
echo "  - Frontend: https://${DOMAIN}"
echo "  - API: https://api.${DOMAIN}"
echo "  - Auth: https://auth.${DOMAIN}"
echo "  - Traefik: https://traefik.${DOMAIN}:8080"
echo ""
EOF

chmod +x scripts/setup-authentik.sh

<function_calls>
<invoke name="TodoWrite">
<parameter name="todos">[{"content": "Add Authentik to docker-compose stack", "status": "completed", "id": "1"}, {"content": "Create Authentik configuration", "status": "completed", "id": "2"}, {"content": "Replace nginx with Traefik reverse proxy", "status": "completed", "id": "3"}, {"content": "Create Authentik initialization scripts", "status": "completed", "id": "4"}, {"content": "Update backend to support Authentik authentication", "status": "in_progress", "id": "5"}, {"content": "Test Authentik integration", "status": "pending", "id": "6"}]