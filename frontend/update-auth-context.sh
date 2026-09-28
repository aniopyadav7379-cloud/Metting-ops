#!/bin/bash
# Batch update frontend components to use AuthentikAuthContext

echo "🔄 Updating frontend components to use AuthentikAuthContext..."

# List of files to update (excluding already updated ones)
files=(
  "src/components/RoleBasedNavigation.tsx"
  "src/components/ProductionDashboard.tsx"
  "src/pages/RecordingPage.tsx"
  "src/AppRoleBased.tsx"
  "src/components/RoleGuard.tsx"
  "src/components/navigation/TopBar.tsx"
  "src/components/SimpleDashboard.tsx"
  "src/components/TestDashboard.tsx"
  "src/components/UnifiedDashboard.tsx"
  "src/components/UserDashboard.tsx"
  "src/components/UserDashboardMinimal.tsx"
  "src/components/UserDashboardSimple.tsx"
  "src/components/UserManagement.tsx"
  "src/components/WebhookManager.tsx"
  "src/components/ProtectedRoute.tsx"
  "src/components/RecordingDashboard.tsx"
  "src/components/RecordingDebug.tsx"
  "src/components/ImprovedDashboard.tsx"
  "src/components/Navigation.tsx"
  "src/components/AccountManagement.tsx"
  "src/components/AdminPanel.tsx"
  "src/components/AudioPlayer.tsx"
  "src/components/AuthDebug.tsx"
  "src/components/AuthErrorHandler.tsx"
  "src/components/CalendarManager.tsx"
  "src/components/DashboardRouter.tsx"
  "src/components/FileManager.tsx"
  "src/Dashboard.tsx"
)

# Update import statements
for file in "${files[@]}"; do
  if [ -f "$file" ]; then
    echo "Updating $file..."
    
    # Replace import statement
    sed -i "s|import { useAuth } from.*AuthContext.*|import { useAuthentikAuth } from '../contexts/AuthentikAuthContext';|g" "$file"
    sed -i "s|import { useAuth } from.*AuthContext.*|import { useAuthentikAuth } from '../../contexts/AuthentikAuthContext';|g" "$file"
    sed -i "s|import { useAuth } from.*AuthContext.*|import { useAuthentikAuth } from '../../../contexts/AuthentikAuthContext';|g" "$file"
    
    # Replace useAuth hooks
    sed -i "s|const { \([^}]*\) } = useAuth()|const { \1 } = useAuthentikAuth()|g" "$file"
    sed -i "s|useAuth()|useAuthentikAuth()|g" "$file"
    
    echo "✅ Updated $file"
  else
    echo "⚠️  File not found: $file"
  fi
done

echo ""
echo "🔄 Updating role checking logic..."

# Files that likely need role logic updates
role_files=(
  "src/components/RoleGuard.tsx"
  "src/components/UserManagement.tsx"
  "src/components/AdminPanel.tsx"
  "src/components/ProtectedRoute.tsx"
)

for file in "${role_files[@]}"; do
  if [ -f "$file" ]; then
    echo "Checking $file for role logic..."
    
    # Check if file has old role checking patterns
    if grep -q "user\.role" "$file" 2>/dev/null; then
      echo "⚠️  $file needs manual role logic update"
      echo "   - Replace user.role checks with user.groups and user.is_admin"
      echo "   - See AuthentikAuthContext.tsx for examples"
    fi
  fi
done

echo ""
echo "✅ Batch update complete!"
echo ""
echo "📋 Manual updates needed:"
echo "1. Review role checking logic in RoleGuard, UserManagement, AdminPanel"
echo "2. Update any user.role references to use user.groups/user.is_admin"
echo "3. Test authentication flow with real Authentik accounts"
echo ""
echo "🔧 Example role checking pattern:"
echo "  const userGroups = user?.groups || [];"
echo "  const isAdmin = user?.is_admin || userGroups.includes('Meeting-Ops-Admins');"