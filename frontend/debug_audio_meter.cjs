#!/usr/bin/env node

/**
 * Audio Meter Debug Script
 * Run with: node debug_audio_meter.cjs
 */

console.log('🔍 Audio Meter Debug Check\n');

// Check environment variables
console.log('📋 Environment Variables:');
const fs = require('fs');
const path = require('path');

try {
  const envPath = path.join(__dirname, '.env');
  const envContent = fs.readFileSync(envPath, 'utf8');
  console.log('   .env file contents:');
  console.log('   ' + envContent.trim());
} catch (e) {
  console.log('   ❌ No .env file found');
}

// Check vite config
console.log('\n⚙️  Vite Configuration:');
try {
  const viteConfigPath = path.join(__dirname, 'vite.config.ts');
  const viteConfig = fs.readFileSync(viteConfigPath, 'utf8');
  
  // Extract proxy configuration
  const proxyMatch = viteConfig.match(/proxy:\s*{([\s\S]*?)}/);
  if (proxyMatch) {
    console.log('   Proxy configuration found:');
    const proxyConfig = proxyMatch[1];
    if (proxyConfig.includes('/ws')) {
      console.log('   ✅ WebSocket proxy configured');
      const wsMatch = proxyConfig.match(/['"]\/ws['"]:\s*{[\s\S]*?target:\s*['"]([^'"]+)['"]/);
      if (wsMatch) {
        console.log(`   WebSocket target: ${wsMatch[1]}`);
      }
    } else {
      console.log('   ❌ WebSocket proxy NOT found');
    }
  }
} catch (e) {
  console.log('   ❌ Could not read vite.config.ts');
}

// Check if components exist
console.log('\n📁 Component Files:');
const componentFiles = [
  'src/components/AudioLevelMeter.tsx',
  'src/components/SimpleDashboard.tsx', 
  'src/components/UserDashboardSimple.tsx'
];

componentFiles.forEach(file => {
  const filePath = path.join(__dirname, file);
  if (fs.existsSync(filePath)) {
    console.log(`   ✅ ${file} exists`);
  } else {
    console.log(`   ❌ ${file} missing`);
  }
});

// Test backend connectivity
console.log('\n🔗 Backend Connectivity:');
const { exec } = require('child_process');

// Test HTTP API
exec('curl -s http://localhost:9051/api/audio-level', (error, stdout, stderr) => {
  if (error) {
    console.log('   ❌ Backend HTTP API not responding');
  } else {
    try {
      const data = JSON.parse(stdout);
      console.log('   ✅ Backend HTTP API working');
      console.log(`   Current audio level: ${data.level}`);
    } catch (e) {
      console.log('   ⚠️  Backend responding but invalid JSON');
    }
  }
  
  console.log('\n📝 Next Steps for AI:');
  console.log('1. Check browser dev tools Network tab for WebSocket connections');
  console.log('2. Add console.log debugging to AudioLevelMeter.tsx');
  console.log('3. Verify component is mounting and attempting connection');
  console.log('4. Test WebSocket connection directly in browser console');
  console.log('\n📋 See FRONTEND_AUDIO_METER_CHECKLIST.md for complete instructions');
});