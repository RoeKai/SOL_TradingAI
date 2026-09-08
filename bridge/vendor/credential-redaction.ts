/**
 * Public credential views.
 *
 * These helpers are deliberately dependency-free so every HTTP/tRPC boundary
 * can use the same representation without ever serialising a usable secret.
 */

export function maskCredential(
  value: string | null | undefined,
  visibleStart = 4,
  visibleEnd = 4,
): string | null {
  const normalized = value?.trim();
  if (!normalized) return null;

  if (normalized.length <= visibleStart + visibleEnd) {
    return `${normalized.slice(0, 1)}••••${normalized.slice(-1)}`;
  }

  return `${normalized.slice(0, visibleStart)}••••${normalized.slice(-visibleEnd)}`;
}

export interface PublicExchangeApiKeyView {
  id: number;
  exchange: string;
  label: string;
  apiKeyConfigured: boolean;
  apiKeyMasked: string | null;
  secretKeyConfigured: boolean;
  passphraseConfigured: boolean;
  isSimulated: number;
  isActive: number;
  createdAt: Date;
  updatedAt: Date;
}

export function toPublicExchangeApiKeyView(input: {
  id: number;
  exchange: string | null;
  label: string;
  apiKey: string | null;
  secretKeyConfigured: boolean;
  passphraseConfigured: boolean;
  isSimulated: number;
  isActive: number;
  createdAt: Date;
  updatedAt: Date;
}): PublicExchangeApiKeyView {
  return {
    id: input.id,
    exchange: input.exchange || "okx",
    label: input.label,
    apiKeyConfigured: Boolean(input.apiKey?.trim()),
    apiKeyMasked: maskCredential(input.apiKey),
    secretKeyConfigured: input.secretKeyConfigured,
    passphraseConfigured: input.passphraseConfigured,
    isSimulated: input.isSimulated,
    isActive: input.isActive,
    createdAt: input.createdAt,
    updatedAt: input.updatedAt,
  };
}

export interface PublicTelegramConfigView {
  botTokenConfigured: boolean;
  botTokenMasked: string | null;
  chatIdConfigured: boolean;
  chatIdMasked: string | null;
  enabled: boolean;
}

export function toPublicTelegramConfigView(input: {
  botToken: string | null | undefined;
  chatId: string | null | undefined;
  enabled: boolean;
}): PublicTelegramConfigView {
  return {
    botTokenConfigured: Boolean(input.botToken?.trim()),
    botTokenMasked: maskCredential(input.botToken, 4, 4),
    chatIdConfigured: Boolean(input.chatId?.trim()),
    chatIdMasked: maskCredential(input.chatId, 2, 3),
    enabled: input.enabled,
  };
}

/** Prevent exchange/client libraries from reflecting submitted credentials. */
export function redactCredentialError(
  message: unknown,
  credentials: Array<string | null | undefined>,
): string {
  let safe = message instanceof Error ? message.message : String(message ?? "Unknown error");
  for (const credential of credentials) {
    if (credential && credential.length >= 3) {
      safe = safe.split(credential).join("[REDACTED]");
    }
  }
  return safe;
}
