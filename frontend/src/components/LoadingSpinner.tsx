import React from 'react';

interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  message?: string;
  className?: string;
}

export const LoadingSpinner: React.FC<LoadingSpinnerProps> = ({ 
  size = 'md', 
  message = 'Loading...', 
  className = '' 
}) => {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8', 
    lg: 'h-12 w-12'
  };

  return (
    <div className={`flex items-center justify-center p-8 ${className}`}>
      <div className="text-center">
        <div className={`animate-spin rounded-full border-b-2 border-purple-600 ${sizeClasses[size]} mx-auto mb-4`}></div>
        <p className="text-gray-400">{message}</p>
      </div>
    </div>
  );
};

export const InlineSpinner: React.FC<{ size?: 'sm' | 'md' }> = ({ size = 'sm' }) => {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-6 w-6'
  };

  return (
    <div className={`animate-spin rounded-full border-2 border-purple-600 border-t-transparent ${sizeClasses[size]}`}></div>
  );
};