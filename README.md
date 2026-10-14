# Hospital Wayfinding Proof of Concept (FIT-549)
Independent FITSOFT demonstration. **NOT an official Ewha Womans University Seoul Hospital service.**
This is a static/mobile five-step QR-to-AR experience using illustrative locations, photographs, distances, and directions. Do not use it to find real clinical care locations or as an evacuation/safety route.

## Demo workflow
1. QR scanning visualization and demonstration start
2. QR-anchored sample origin confirmation
3. Destination selection (sample B12/B14/B15)
4. Image-aided sample route preview
5. Mobile camera directional overlay and optional 8th Wall SLAM world-tracking experiment, then explicitly *manual* arrival preview

The arrival screen is not a physical positioning result. The default live-camera arrow is a **2D overlay**. It does not classify walls/doors, avoid obstacles, or assert traversability. The 8th Wall markerless mode attempts world tracking; its actual device reliability has not been verified on iPhone/Android and the floor alone is not a safe travel path.

## Deployment
Static HTML/CSS/JS; no patient information or server-side database. Intended for HTTPS hosting (camera requires secure context).
Published using GitHub Pages; all assets use relative URLs. The world-tracking engine is an unmodified vendor binary from the existing FIT-549 PoC.

## Sources and license
- Corridor photos corridor-1/2/3: W.carter, Wikimedia Commons, CC0. These are not photographs of Ewha Womans University Seoul Hospital.
- bright-corridor.jpg: Unsplash reference image, not the hospital.
- 8th Wall XR Engine © 2026 Niantic Spatial, Inc. Subject to the binary license at external/xr/LICENSE (also https://github.com/8thwall/engine/blob/main/LICENSE). Use of the vendor engine is an optional component of a broader app prototype.
- Source code is for demo use, no hidden credentials or analytics.

## 2026-10-14 AR camera framing correction
The AR camera is now displayed in a 3:4 portrait viewport instead of stretching the renderer over the entire tall phone screen. The regular camera preview uses `object-fit: contain` to avoid unintended cropping. The XR WebGL drawing buffer is initialized only after the canvas is visible and uses the camera viewport dimensions. This prevents a 1x1 WebGL canvas on first XR startup. The fix changes screen framing, not the physical camera optics; real iPhone/Android camera FoV and SLAM quality still require device verification.
