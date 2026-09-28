#!/bin/bash
# Fix TypeScript errors by removing unused imports and variables

echo "Fixing TypeScript errors..."

# Fix App-complex.tsx
sed -i "s/const source = error.source || 'Unknown';/\/\/ const source = error.source || 'Unknown';/" src/App-complex.tsx

# Fix Dashboard.tsx
sed -i "s/import { NPUModelSettings } from '.\/components\/NPUModelSettings';/\/\/ import { NPUModelSettings } from '.\/components\/NPUModelSettings';/" src/Dashboard.tsx
sed -i "s/const \[recentTranscriptions, setRecentTranscriptions\]/\/\/ const [recentTranscriptions, setRecentTranscriptions]/" src/Dashboard.tsx
sed -i "s/const \[isConnected, setIsConnected\]/\/\/ const [isConnected, setIsConnected]/" src/Dashboard.tsx

# Fix SessionDetails.tsx
sed -i "s/\.map((trans, index) =>/\.map((trans) =>/" src/SessionDetails.tsx

# Fix AIAgents.tsx
sed -i "s/Pause, RefreshCw, AlertCircle,/\/\/ Pause, RefreshCw, AlertCircle,/" src/components/AIAgents.tsx
sed -i "s/, Copy//" src/components/AIAgents.tsx
sed -i "s/const { data } = response;/\/\/ const { data } = response;/" src/components/AIAgents.tsx

# Fix AIModelsManager.tsx
sed -i "s/, Battery, Info//" src/components/AIModelsManager.tsx
sed -i "s/, HardDrive,/, \/\/ HardDrive,/" src/components/AIModelsManager.tsx
sed -i "s/, MessageSquare//" src/components/AIModelsManager.tsx
sed -i "s/Play, Pause,/Play, \/\/ Pause,/" src/components/AIModelsManager.tsx
sed -i "s/, Check//" src/components/AIModelsManager.tsx
sed -i "s/const { data } = response;/\/\/ const { data } = response;/" src/components/AIModelsManager.tsx

# Fix AccountManagement.tsx
sed -i "s/, Mail//" src/components/AccountManagement.tsx
sed -i "s/import { Tooltip } from '.\/ui\/tooltip';/\/\/ import { Tooltip } from '.\/ui\/tooltip';/" src/components/AccountManagement.tsx
sed -i "s/const { onClose } = props;/\/\/ const { onClose } = props;/" src/components/AccountManagement.tsx

# Fix AdminPanel.tsx
sed -i "s/const adminSections/\/\/ const adminSections/" src/components/AdminPanel.tsx

# Fix AudioLevelMeter.tsx
sed -i "s/, wsUrl//" src/components/AudioLevelMeter.tsx
sed -i "s/, setSpectrum//" src/components/AudioLevelMeter.tsx

# Fix CalendarManager.tsx
sed -i "s/, Clock, Users,/, \/\/ Clock, Users,/" src/components/CalendarManager.tsx
sed -i "s/, Settings//" src/components/CalendarManager.tsx
sed -i "s/const \[meetings, setMeetings\]/\/\/ const [meetings, setMeetings]/" src/components/CalendarManager.tsx
sed -i "s/\.map(c => c.name)/\.map((c: any) => c.name)/" src/components/CalendarManager.tsx

# Fix DashboardRouter.tsx
sed -i "s/import { TestDashboard } from '.\/TestDashboard';/\/\/ import { TestDashboard } from '.\/TestDashboard';/" src/components/DashboardRouter.tsx
sed -i "s/import { UserDashboardMinimal } from '.\/UserDashboardMinimal';/\/\/ import { UserDashboardMinimal } from '.\/UserDashboardMinimal';/" src/components/DashboardRouter.tsx
sed -i "s/import { TestMinimal } from '.\/TestMinimal';/\/\/ import { TestMinimal } from '.\/TestMinimal';/" src/components/DashboardRouter.tsx
sed -i "s/import { PlainHtmlTest } from '.\/PlainHtmlTest';/\/\/ import { PlainHtmlTest } from '.\/PlainHtmlTest';/" src/components/DashboardRouter.tsx
sed -i "s/const switchToUser/\/\/ const switchToUser/" src/components/DashboardRouter.tsx
sed -i "s/const switchToLegacy/\/\/ const switchToLegacy/" src/components/DashboardRouter.tsx

# Fix EmailNotifications.tsx
sed -i "s/, Users, Calendar, Clock//" src/components/EmailNotifications.tsx
sed -i "s/onClose/\/\/ onClose/" src/components/EmailNotifications.tsx

# Fix HelpModal.tsx
sed -i "s/import { Button } from '.\/ui\/button';/\/\/ import { Button } from '.\/ui\/button';/" src/components/HelpModal.tsx

# Fix IntelligentAssistant.tsx
sed -i "s/, Clock//" src/components/IntelligentAssistant.tsx
sed -i "s/, User//" src/components/IntelligentAssistant.tsx

echo "TypeScript errors fixed!"
echo "Running build to verify..."
npm run build