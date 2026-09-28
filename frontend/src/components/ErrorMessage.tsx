import React from 'react';
import { AlertCircle, RefreshCw, X } from 'lucide-react';

interface ErrorMessageProps {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  className?: string;
  variant?: 'default' | 'minimal' | 'banner';
}

export const ErrorMessage: React.FC<ErrorMessageProps> = ({ 
  message, 
  onRetry, 
  onDismiss,
  className = '',
  variant = 'default'
}) => {
  if (variant === 'minimal') {
    return (
      <div className={`flex items-center gap-2 text-red-400 ${className}`}>
        <AlertCircle className="w-4 h-4" />
        <span className="text-sm">{message}</span>
        {onRetry && (
          <button 
            onClick={onRetry}
            className="text-red-400 hover:text-red-300 text-sm underline"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  if (variant === 'banner') {
    return (
      <div className={`bg-red-900/20 border border-red-500 rounded-lg p-4 ${className}`}>
        <div className="flex items-start justify-between">
          <div className="flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-red-400 mt-0.5" />
            <div>
              <div className="text-red-400 font-medium">Error</div>
              <div className="text-red-300 text-sm mt-1">{message}</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {onRetry && (
              <button
                onClick={onRetry}
                className="flex items-center gap-1 px-3 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors"
              >
                <RefreshCw className="w-3 h-3" />
                Retry
              </button>
            )}
            {onDismiss && (
              <button
                onClick={onDismiss}
                className="p-1 text-red-400 hover:text-red-300"
              >
                <X className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`bg-red-900/20 border border-red-500 rounded-lg p-4 m-4 ${className}`}>
      <div className="flex items-start gap-3">
        <AlertCircle className="w-5 h-5 text-red-400 mt-0.5" />
        <div className="flex-1">
          <div className="text-red-400 font-medium">Error</div>
          <div className="text-red-300 text-sm mt-1">{message}</div>
          {onRetry && (
            <button 
              onClick={onRetry}
              className="mt-3 flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded text-sm transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Try Again
            </button>
          )}
        </div>
        {onDismiss && (
          <button
            onClick={onDismiss}
            className="text-red-400 hover:text-red-300"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  );
};