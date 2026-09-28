import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi } from 'vitest';

import AudioDeviceList from '../components/rooms/AudioDeviceList';

const roomsApiMock = vi.hoisted(() => ({
  listAudioDevices: vi.fn(),
  probeAudioDevice: vi.fn(),
}));

vi.mock('../services/roomsApi', () => ({
  default: {
    listAudioDevices: roomsApiMock.listAudioDevices,
    probeAudioDevice: roomsApiMock.probeAudioDevice,
  },
}));

const DEVICES = [
  {
    device_path: 'hw:2,0',
    card_name: 'USB Audio Device',
    device_name: 'Podium Mic',
  },
  {
    device_path: 'hw:3,0',
    card_name: 'Ceiling Array',
    device_name: 'Audience Mic',
  },
];

function MultiSelectHarness() {
  const [selected, setSelected] = useState<string[]>([]);
  return (
    <AudioDeviceList
      orgSlug="test-org"
      selectionMode="multiple"
      selectedDevicePaths={selected}
      onSelectionChange={(devices) => setSelected(devices.map((device) => device.device_path))}
      disabledDevicePaths={['hw:9,0']}
    />
  );
}

describe('AudioDeviceList', () => {
  beforeEach(() => {
    roomsApiMock.listAudioDevices.mockResolvedValue(DEVICES);
    roomsApiMock.probeAudioDevice.mockResolvedValue({
      device_path: 'hw:2,0',
      duration_sec: 3,
      sample_count: 48000,
      rms_db: -27,
      peak_db: -10,
      detected_audio: true,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('supports multi-select with per-row test buttons', async () => {
    render(<MultiSelectHarness />);

    await waitFor(() => expect(screen.getByText('Podium Mic')).toBeInTheDocument());

    expect(screen.getAllByRole('button', { name: /test mic/i })).toHaveLength(2);

    const podiumButton = screen.getByText('Podium Mic').closest('button');
    const audienceButton = screen.getByText('Audience Mic').closest('button');
    expect(podiumButton).toBeTruthy();
    expect(audienceButton).toBeTruthy();

    fireEvent.click(podiumButton!);
    expect(podiumButton).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(audienceButton!);
    expect(podiumButton).toHaveAttribute('aria-pressed', 'true');
    expect(audienceButton).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getAllByRole('button', { name: /test mic/i })[0]);
    await waitFor(() =>
      expect(roomsApiMock.probeAudioDevice).toHaveBeenCalledWith(
        'hw:2,0',
        expect.any(Object),
      ),
    );
  });
});
