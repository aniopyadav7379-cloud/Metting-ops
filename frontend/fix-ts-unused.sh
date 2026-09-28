#!/bin/bash
# Fix TypeScript unused variable errors

echo "Fixing TypeScript unused variable errors..."

# Fix App.tsx - Dashboard import is used in DashboardRouter
# Let's check if Dashboard is actually used in the legacy route
if ! grep -q "Dashboard" src/components/DashboardRouter.tsx; then
  sed -i '/^import Dashboard from/d' src/App.tsx
fi

# Fix Dashboard.tsx
sed -i '/^import { NPUModelSettings }/d' src/Dashboard.tsx
sed -i 's/const \[recentTranscriptions, setRecentTranscriptions\] = useState/\/\/ const [recentTranscriptions, setRecentTranscriptions] = useState/' src/Dashboard.tsx
sed -i 's/const \[isConnected, setIsConnected\] = useState/\/\/ const [isConnected, setIsConnected] = useState/' src/Dashboard.tsx

# Fix SessionDetails.tsx - remove unused index parameter
sed -i 's/\.map((trans, index) =>/\.map((trans) =>/' src/SessionDetails.tsx

# Fix AIAgents.tsx
sed -i 's/Play, Pause, RefreshCw, AlertCircle,/Play,/' src/components/AIAgents.tsx
sed -i 's/Settings, Copy, CheckCircle/Settings, CheckCircle/' src/components/AIAgents.tsx
sed -i 's/const { data } = response;/\/\/ const { data } = response;/' src/components/AIAgents.tsx

# Fix AIModelsManager.tsx  
sed -i 's/Download, Upload, Battery, Info/Download, Upload/' src/components/AIModelsManager.tsx
sed -i 's/Trash2, RefreshCw, HardDrive, Zap, Brain, MessageSquare/Trash2, RefreshCw, Zap, Brain/' src/components/AIModelsManager.tsx
sed -i 's/Play, Pause, Power, Settings, Loader, Check/Play, Power, Settings, Loader/' src/components/AIModelsManager.tsx
sed -i 's/const { data } = response;/\/\/ const { data } = response;/' src/components/AIModelsManager.tsx

# Fix AccountManagement.tsx
sed -i 's/User, Mail, Lock/User, Lock/' src/components/AccountManagement.tsx
sed -i '/^import { Tooltip }/d' src/components/AccountManagement.tsx
sed -i 's/export const AccountManagement = (props: any) => {/export const AccountManagement = () => {/' src/components/AccountManagement.tsx
sed -i 's/const { onClose } = props;/\/\/ const { onClose } = props;/' src/components/AccountManagement.tsx

# Fix AdminPanel.tsx
sed -i 's/import React, { useState, useEffect }/import React, { useState }/' src/components/AdminPanel.tsx
sed -i 's/Activity,//' src/components/AdminPanel.tsx
sed -i 's/Server,//' src/components/AdminPanel.tsx
sed -i 's/Wifi,//' src/components/AdminPanel.tsx
sed -i 's/Database,//' src/components/AdminPanel.tsx
sed -i 's/Lock,//' src/components/AdminPanel.tsx
sed -i 's/FileText,//' src/components/AdminPanel.tsx
sed -i 's/systemStatus, setSystemStatus/\/\/ systemStatus, setSystemStatus/' src/components/AdminPanel.tsx

# Fix App-complex.tsx
sed -i 's/const source = error.source || '\''Unknown'\'';/\/\/ const source = error.source || '\''Unknown'\'';/' src/App-complex.tsx

# Fix CalendarManager.tsx
sed -i 's/Calendar, Clock, Users,/Calendar,/' src/components/CalendarManager.tsx
sed -i 's/AlertCircle, Plus, Settings/AlertCircle, Plus/' src/components/CalendarManager.tsx
sed -i 's/const \[meetings, setMeetings\] = useState/\/\/ const [meetings, setMeetings] = useState/' src/components/CalendarManager.tsx

# Fix DashboardRouter.tsx
sed -i 's/const switchToUser/\/\/ const switchToUser/' src/components/DashboardRouter.tsx
sed -i 's/const switchToLegacy/\/\/ const switchToLegacy/' src/components/DashboardRouter.tsx

# Fix EmailNotifications.tsx
sed -i 's/Send, Mail, Users, Calendar, Clock,/Send, Mail,/' src/components/EmailNotifications.tsx

# Fix HelpModal.tsx
sed -i '/^import { Button }/d' src/components/HelpModal.tsx

# Fix IntelligentAssistant.tsx
sed -i 's/Bot, Sparkles, X, Send, Clock/Bot, Sparkles, X, Send/' src/components/IntelligentAssistant.tsx
sed -i 's/Minimize2, User/Minimize2/' src/components/IntelligentAssistant.tsx

# Fix AudioLevelMeter.tsx
sed -i 's/export const AudioLevelMeter = ({ sessionId, wsUrl }: { sessionId?: string; wsUrl?: string })/export const AudioLevelMeter = ({ sessionId }: { sessionId?: string })/' src/components/AudioLevelMeter.tsx
sed -i 's/const \[spectrum, setSpectrum\]/const [spectrum]/' src/components/AudioLevelMeter.tsx

# Fix vite.config.ts
sed -i 's/(req, res, next)/(_, _, next)/' vite.config.ts
sed -i 's/(proxyReq, req, res)/(_, _, _)/' vite.config.ts

echo "Running build to verify fixes..."
npm run build 2>&1 | tail -20