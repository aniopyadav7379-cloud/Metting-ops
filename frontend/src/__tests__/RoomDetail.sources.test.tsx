import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import RoomDetail from '../pages/RoomDetail';

const roomsApiMock = vi.hoisted(() => ({
  get: vi.fn(),
  listSources: vi.fn(),
  listAcl: vi.fn(),
  listOrgUsers: vi.fn(),
  removeSource: vi.fn(),
  update: vi.fn(),
  generatePairingCode: vi.fn(),
  redeemPairingCode: vi.fn(),
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { is_superuser: false },
  }),
}));

vi.mock('../contexts/OrgContext', () => ({
  useOrg: () => ({
    activeOrganization: {
      id: 1,
      slug: 'test-org',
      name: 'Test Org',
      role: 'admin',
    },
  }),
}));

vi.mock('../components/rooms/AudioDeviceList', () => ({
  default: () => <div data-testid="audio-device-list" />,
}));

vi.mock('../components/ConfirmModal', () => ({
  default: () => null,
}));

vi.mock('../components/rooms/PairingCodeDisplay', () => ({
  default: () => null,
}));

vi.mock('../components/rooms/DeviceSecretReveal', () => ({
  default: () => null,
}));

vi.mock('../components/rooms/RoomLevelMeter', () => ({
  default: () => <div data-testid="room-level-meter" />,
}));

vi.mock('../components/rooms/RoomLiveTranscript', () => ({
  default: () => <div data-testid="room-live-transcript" />,
}));

vi.mock('../components/rooms/RoomLiveSummary', () => ({
  default: () => <div data-testid="room-live-summary" />,
}));

vi.mock('../services/roomsApi', () => ({
  default: {
    get: roomsApiMock.get,
    listSources: roomsApiMock.listSources,
    listAcl: roomsApiMock.listAcl,
    listOrgUsers: roomsApiMock.listOrgUsers,
    removeSource: roomsApiMock.removeSource,
    update: roomsApiMock.update,
    generatePairingCode: roomsApiMock.generatePairingCode,
    redeemPairingCode: roomsApiMock.redeemPairingCode,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    remove: vi.fn(),
    grantAcl: vi.fn(),
    revokeAcl: vi.fn(),
    list: vi.fn(),
    listAudioDevices: vi.fn(),
    probeAudioDevice: vi.fn(),
    getActiveSession: vi.fn(),
    discardSession: vi.fn(),
  },
}));

const ROOM = {
  id: 42,
  raw_id: 'room-1',
  organization_id: 1,
  name: 'Board Room',
  location: 'HQ',
  description: null,
  status: 'idle',
  recording_mode: 'manual',
  retention_days: 90,
  legal_hold: false,
  current_session_id: null,
  current_session_started_at: null,
  last_recording_at: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
};

const SOURCES = [
  {
    id: 1,
    raw_id: 'source-1',
    room_id: 42,
    hardware_type: 'server_usb_mic',
    device_path: 'hw:2,0',
    device_id: 'usb-1',
    label: 'Podium mic',
    status: 'idle',
    is_active: true,
    health_status: 'healthy',
    config_json: null,
    created_at: '2026-05-01T00:00:00Z',
  },
  {
    id: 2,
    raw_id: 'source-2',
    room_id: 42,
    hardware_type: 'server_usb_mic',
    device_path: 'hw:3,0',
    device_id: 'usb-2',
    label: 'Audience mic',
    status: 'recording',
    is_active: true,
    health_status: 'healthy',
    config_json: null,
    created_at: '2026-05-01T00:00:00Z',
  },
  {
    id: 3,
    raw_id: 'source-3',
    room_id: 42,
    hardware_type: 'server_usb_mic',
    device_path: 'hw:4,0',
    device_id: 'usb-3',
    label: 'Spare mic',
    status: 'disabled',
    is_active: false,
    health_status: 'offline',
    config_json: null,
    created_at: '2026-05-01T00:00:00Z',
  },
  {
    id: 4,
    raw_id: 'source-4',
    room_id: 42,
    hardware_type: 'server_usb_mic',
    device_path: 'hw:5,0',
    device_id: 'usb-4',
    label: 'Problem mic',
    status: 'error',
    is_active: true,
    health_status: 'error',
    config_json: null,
    created_at: '2026-05-01T00:00:00Z',
  },
];

describe('RoomDetail sources', () => {
  beforeEach(() => {
    const storage: Record<string, string> = {};
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => storage[key] ?? null),
      setItem: vi.fn((key: string, value: string) => {
        storage[key] = value;
      }),
      removeItem: vi.fn((key: string) => {
        delete storage[key];
      }),
      clear: vi.fn(() => {
        Object.keys(storage).forEach((key) => delete storage[key]);
      }),
    });
    roomsApiMock.get.mockResolvedValue(ROOM);
    roomsApiMock.listSources.mockResolvedValue(SOURCES);
    roomsApiMock.listAcl.mockResolvedValue([]);
    roomsApiMock.listOrgUsers.mockResolvedValue([]);
    roomsApiMock.removeSource.mockResolvedValue(undefined);
    roomsApiMock.update.mockResolvedValue(ROOM);
    roomsApiMock.generatePairingCode.mockResolvedValue(null);
    roomsApiMock.redeemPairingCode.mockResolvedValue(null);
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it('shows source statuses and removes a source from settings', async () => {
    render(
      <MemoryRouter initialEntries={['/rooms/room-1']}>
        <Routes>
          <Route path="/rooms/:id" element={<RoomDetail />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText('Board Room')).toBeInTheDocument());

    // v3.19: room tab buttons now use role="tab" (ARIA tablist).
    fireEvent.click(screen.getByRole('tab', { name: /settings/i }));

    expect(screen.getByText('Podium mic')).toBeInTheDocument();
    expect(screen.getByText('Audience mic')).toBeInTheDocument();
    expect(screen.getByText('Spare mic')).toBeInTheDocument();
    expect(screen.getByText('Problem mic')).toBeInTheDocument();

    expect(screen.getByText('idle')).toBeInTheDocument();
    expect(screen.getByText('recording')).toBeInTheDocument();
    expect(screen.getByText('disabled')).toBeInTheDocument();
    expect(screen.getByText('error')).toBeInTheDocument();

    fireEvent.click(screen.getAllByLabelText(/remove source/i)[0]);

    await waitFor(() =>
      expect(roomsApiMock.removeSource).toHaveBeenCalledWith('room-1', 'source-1', {
        orgSlug: 'test-org',
      }),
    );
  });
});
