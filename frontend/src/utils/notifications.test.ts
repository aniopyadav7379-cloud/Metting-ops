import { vi } from 'vitest';
import { showNotification, showError, showSuccess, showInfo } from './notifications';
import { showToast } from '../components/Toast';

vi.mock('../components/Toast', () => ({
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  },
}));

describe('notifications', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('showNotification routes to the matching toast type', () => {
    showNotification('Saved', 'success');
    expect(showToast.success).toHaveBeenCalledWith('Saved');

    showNotification('Broke', 'error');
    expect(showToast.error).toHaveBeenCalledWith('Broke');

    showNotification('FYI', 'info');
    expect(showToast.info).toHaveBeenCalledWith('FYI');
  });

  it('showNotification defaults to info', () => {
    showNotification('Plain message');
    expect(showToast.info).toHaveBeenCalledWith('Plain message');
  });

  it('showNotification does not call window.alert', () => {
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    showNotification('Saved', 'success');
    expect(alertSpy).not.toHaveBeenCalled();
    alertSpy.mockRestore();
  });

  it('showError extracts the message and prefixes context', () => {
    showError(new Error('upload exploded'), 'Upload failed');
    expect(showToast.error).toHaveBeenCalledWith('Upload failed: upload exploded');
  });

  it('showError works without context', () => {
    showError(new Error('upload exploded'));
    expect(showToast.error).toHaveBeenCalledWith('upload exploded');
  });

  it('showSuccess and showInfo delegate to their toast types', () => {
    showSuccess('Done');
    expect(showToast.success).toHaveBeenCalledWith('Done');

    showInfo('Heads up');
    expect(showToast.info).toHaveBeenCalledWith('Heads up');
  });
});
