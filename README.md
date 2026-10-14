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

## 2026-10-14 (3차) 승인 시안에 맞춘 전체 화면 카메라
3:4 작은 틀이 시안(카메라가 화면 전체, 안내 카드는 카메라 위에 겹침)과 달라, 카메라를 화면 상단부터 하단 패널 직전까지 가득 채우는 방식(`object-fit: cover`)으로 되돌렸다. 390x844 기준 카메라 영역 390x659, 3:4 영상 기준 가로 약 79%가 보인다(`?debug=1`의 visibleWidth). 즉 틀 안에 전체 시야를 보이던 2차 방식보다 가장자리는 조금 잘리고, 1차 이전(전체 화면 약 62%)보다는 넓다. 실기기 검증 전.

## 2026-10-14 (4차) 시안 4번처럼 카메라 전체 화면
사용자 제공 고객용 시안 5장 중 4번(AR)·5번(도착)과 맞추어, AR 카메라를 화면 전체(390x844)에 `cover`로 채우고 안내 카드·버튼·화살표를 그 위에 겹쳤다. 3:4 영상 기준 가로 약 62%가 보인다(`?debug=1`의 visibleWidth) — 화면 전체를 채우는 방식의 구조적 대가이며 실기기 확인 전.

## 2026-10-14 (5차) 고객용 시안 1·2·3·5번에 맞춘 화면 정리
QR 스캔(QR 틀·하단 시트), 목적지 선택(주요 진료과/검사·치료/편의시설 탭, 거리 표시, "이곳으로 이동하기"), 경로 안내(출발·목적지 카드와 변경 버튼, 번호 선이 이어진 사진 단계, "AR로 안내받기"/"지도 보기"), 도착(전체 사진 위 카드, "처음으로 돌아가기")을 시안 구성에 맞췄다. 목적지 8곳과 거리·시간은 모두 시연용 샘플이며, 병원 공식 로고·실제 병원 사진은 사용 승인이 없어 쓰지 않았다(비공식 표기와 참고 사진 유지).

## 2026-10-14 (6차) 8th Wall 모드 확대 해소
8th Wall 모드에서 XR 캔버스를 화면 전체로 채우면 엔진이 4:3 영상을 cover로 잘라 약 1.6배 확대돼 보였다. 8th Wall 모드는 캔버스를 영상 비율(3:4)로 가로 전체 폭에 맞춰 화면 중앙에 두어 영상이 잘리지 않게 했다(390x844에서 390x520, 위아래는 어두운 배경). 일반 카메라 모드는 시안대로 전체 화면 유지. 실기기 검증 전.

## 7차 (FIT-549) 바닥 인식 기반 화살표
- 8th Wall 모드에서 카메라 프레임을 기기 안에서 분석(DeepLab ADE20K, tfjs, `assets/seg/`)해 바닥으로 보이는 쪽에만 화살표를 놓는다. 영상은 전송·저장하지 않는다.
- 벽만 보이거나 바닥이 거의 없으면 화살표를 숨기고 "앞이 막혔어요 · 바닥 쪽으로 돌려 주세요"를 표시한다. 카메라 미리보기 모드도 같은 분석으로 화살표 방향·숨김을 정한다.
- 한계: 지면 높이는 SLAM 카메라 높이(없으면 1.4m 가정) 기준이라 실제 바닥과 오차가 있을 수 있다. 모델(ADE20K 가중치)의 상용 사용 라이선스는 미확인. 실기기(iPhone Safari/Android Chrome) 속도·정확도는 미검증(헤드리스 에뮬레이션만).
- 진단: `?step=5&debug=1`에서 분석 썸네일과 상태 표시.
