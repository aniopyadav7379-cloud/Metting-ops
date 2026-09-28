import React from 'react';

export const SimpleTest: React.FC = () => {
  return (
    <div style={{
      minHeight: '100vh',
      backgroundColor: '#111827',
      color: 'white',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      flexDirection: 'column',
      fontFamily: 'Arial, sans-serif'
    }}>
      <h1 style={{ fontSize: '3rem', marginBottom: '1rem' }}>
        ✅ React is Working!
      </h1>
      <p style={{ fontSize: '1.5rem', color: '#9ca3af', marginBottom: '2rem' }}>
        Unicorn Commander Meeting-Ops Dashboard
      </p>
      <div style={{
        padding: '2rem',
        backgroundColor: '#374151',
        borderRadius: '12px',
        boxShadow: '0 10px 25px rgba(0,0,0,0.3)'
      }}>
        <p style={{ marginBottom: '1rem' }}>🌐 Server: 192.168.1.223:7777</p>
        <p style={{ marginBottom: '1rem' }}>⏰ Time: {new Date().toLocaleString()}</p>
        <p>🚀 Ready for Full Dashboard</p>
      </div>
    </div>
  );
};

export default SimpleTest;