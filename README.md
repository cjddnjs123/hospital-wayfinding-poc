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

## 2026-10-14 (2차) 카메라 확대 원인 보정과 추적 안정화
- 원인: 8th Wall 엔진은 4:3 카메라 영상을 받아 캔버스에 "채우기(cover)" 방식으로 그린다. 세로 전체 화면(390x844) 캔버스에서는 영상 가로가 약 38% 잘려 약 1.6배 확대돼 보였다(이전 수정으로 3:4 화면에서 해소). 일반 카메라 모드는 1280x720(16:9)을 요청해 세로 화면에서 센서 가로 시야가 약 25% 줄 수 있었다.
- 일반 카메라는 4:3(1280x960) 전체 센서 영상을 요청하고(`aspectRatio`, `resizeMode:none`), 실제 영상 비율에 맞춰 화면 틀 비율을 자동 조정한다.
- 8th Wall 모드는 엔진이 보고하는 영상 크기에 맞춰 캔버스 비율을 맞춘다. 추적이 확인되기 전·추적 손실 때에는 화살표를 숨기고, 손실 후 복구되면 새로 배치한다. 10~12초 안에 추적을 못 얻으면 카메라 2D 예시 모드로 복귀한다.
- 카메라 권한 거부·기기 없음·사용 중 오류 문구를 구분했다. 화면이 숨겨지면 카메라를 끄고, 중복 시작을 막으며, 엔진 정지 시 파이프라인 모듈과 재질을 정리한다.
- `?debug=1`을 붙이면 영상 해상도·비율·줌 범위·XR 캔버스·FOV를 화면에 표시해 실기기 확대 원인을 확인할 수 있다.
- 검증: 브라우저 모의 카메라(3:4, 9:16)와 모의 8th Wall 전환 자동 검사만 통과. **실제 iPhone Safari·Android Chrome 검증은 아직 없음.**
