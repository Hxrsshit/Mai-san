import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Required for the Docker image: emits a minimal standalone server bundle.
  output: "standalone",
};

export default nextConfig;
