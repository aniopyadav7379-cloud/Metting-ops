import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { vi } from 'vitest';
import { Login } from '../components/Login';

const loginMock = vi.fn();

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    login: loginMock,
    isAuthenticated: false,
    isLoading: false,
  }),
}));

describe('Login', () => {
  beforeEach(() => {
    loginMock.mockReset();
  });

  it('shows the verified-email banner when verify=success is present', () => {
    render(
      <MemoryRouter initialEntries={['/login?verify=success']}>
        <Routes>
          <Route path="/login" element={<Login />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText(/email verified, please sign in/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in with unicorn commander/i })).toBeInTheDocument();
  });

  it('shows the resend flow when verify=invalid is present', () => {
    render(
      <MemoryRouter initialEntries={['/login?verify=invalid']}>
        <Routes>
          <Route path="/login" element={<Login />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText(/verification link was invalid or expired/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /resend/i })).toBeInTheDocument();
  });
});
