/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface DocumentPictureInPicture {
  requestWindow(options?: {
    width?: number;
    height?: number;
    disallowReturnToOpener?: boolean;
  }): Promise<Window & typeof globalThis>;
  window?: (Window & typeof globalThis) | null;
}

interface Window {
  documentPictureInPicture?: DocumentPictureInPicture;
}
