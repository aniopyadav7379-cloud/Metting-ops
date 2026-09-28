/**
 * Utility functions for proper error handling
 */

import { showToast } from '../components/Toast';

/**
 * Safely converts an error to a displayable string message
 * @param error - The error object, which could be an Error instance, string, or any other type
 * @param defaultMessage - Default message if error cannot be properly stringified
 * @returns A user-friendly error message
 */
export function getErrorMessage(error: unknown, defaultMessage: string = 'An unknown error occurred'): string {
  if (error instanceof Error) {
    return error.message;
  }
  
  if (typeof error === 'string') {
    return error;
  }
  
  if (error && typeof error === 'object') {
    // Check if it has a message property
    if ('message' in error && typeof error.message === 'string') {
      return error.message;
    }
    
    // Check if it has a detail property (common in API responses)
    if ('detail' in error && typeof error.detail === 'string') {
      return error.detail;
    }
    
    // Check if it has an error property
    if ('error' in error && typeof error.error === 'string') {
      return error.error;
    }
    
    // Try to stringify if it's a simple object
    try {
      const str = JSON.stringify(error);
      if (str !== '{}') {
        return str;
      }
    } catch {
      // If stringify fails, fall through to default
    }
  }
  
  return defaultMessage;
}

/**
 * Shows an error toast with proper error message handling
 * @param error - The error to display
 * @param prefix - Optional prefix for the error message
 */
export function showErrorAlert(error: unknown, prefix: string = 'Error'): void {
  const message = getErrorMessage(error);
  showToast.error(`${prefix}: ${message}`);
}