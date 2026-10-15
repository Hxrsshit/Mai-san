/** Turning a caught error into something a person can read and act on. */

export interface ErrorView {
  message: string;
  /** Worth offering "Try again". Auth and not-found errors are not. */
  retryable: boolean;
  /** The person must sign in; retrying alone will not help. */
  needsSignIn: boolean;
}

interface ApiLike {
  message: string;
  code: string;
  status: number;
}

function isApiLike(value: unknown): value is ApiLike {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as ApiLike).code === "string" &&
    typeof (value as ApiLike).status === "number" &&
    typeof (value as ApiLike).message === "string"
  );
}

export function describeError(caught: unknown): ErrorView {
  if (!isApiLike(caught)) {
    return { message: "Something went wrong. Please try again.", retryable: true, needsSignIn: false };
  }
  const needsSignIn =
    caught.code === "authentication_cancelled" ||
    caught.code === "authentication_required" ||
    caught.code === "invalid_credentials";
  // A 4xx other than a rate limit will fail the same way again.
  const retryable =
    caught.code === "network_error" ||
    caught.status === 0 ||
    caught.status >= 500 ||
    caught.status === 429 ||
    needsSignIn;
  return { message: caught.message, retryable, needsSignIn };
}

/** True when this failure means the backend never answered. */
export function isUnreachable(caught: unknown): boolean {
  return isApiLike(caught) && (caught.code === "network_error" || caught.status === 0);
}
