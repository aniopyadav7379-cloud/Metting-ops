import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';

vi.mock('../utils/posthog', () => ({ track: vi.fn() }));

function Broken() {
  throw new Error('render failed');
}

test('renders recovery controls when a child throws', () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  render(
    <ErrorBoundary>
      <Broken />
    </ErrorBoundary>,
  );

  expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong');
  expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reset app data' })).toBeInTheDocument();
  consoleError.mockRestore();
});

test('reset clears browser state', () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  const localClear = vi.fn();
  const sessionClear = vi.fn();
  Object.defineProperty(window, 'localStorage', { configurable: true, value: { clear: localClear } });
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: { clear: sessionClear } });
  render(<ErrorBoundary><Broken /></ErrorBoundary>);

  fireEvent.click(screen.getByRole('button', { name: 'Reset app data' }));
  expect(localClear).toHaveBeenCalledOnce();
  expect(sessionClear).toHaveBeenCalledOnce();
  consoleError.mockRestore();
});
