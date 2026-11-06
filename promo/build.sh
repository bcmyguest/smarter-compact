#!/bin/sh
# Rebuild the promo assets in promo/out from scene.html. Needs Playwright (Chromium) and ffmpeg.
set -e
cd "$(dirname "$0")"
FRAMES=$(mktemp -d); CARDS=$(mktemp -d); mkdir -p out
node render-frames.cjs "$PWD/scene.html" "$FRAMES" 30
ffmpeg -y -loglevel error -framerate 30 -i "$FRAMES/f%04d.png" -c:v libx264 -pix_fmt yuv420p -crf 18 -preset slow -movflags +faststart out/smarter-compact-demo.mp4
# The GIF uses a still background: drifting dots change every pixel, which bloats it and makes dithering shimmer.
GIFFRAMES=$(mktemp -d)
node render-frames.cjs "$PWD/scene.html" "$GIFFRAMES" 20 stillbg
ffmpeg -y -loglevel error -framerate 20 -i "$GIFFRAMES/f%04d.png" -vf "fps=20,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle" -loop 0 out/smarter-compact-demo.gif
node render-cards.cjs "$PWD/scene.html" "$CARDS" card=hero card=gh card=results card=chart
cp "$CARDS/card_hero.png" out/card-hero-2400x1350.png
cp "$CARDS/card_results.png" out/card-results-2400x1350.png
cp "$CARDS/card_chart.png" out/card-chart-2400x1350.png
cp "$CARDS/card_gh.png" out/github-social-preview-1280x640.png
# Terminal demo: a session with prompts, a trim, and recall from the store.
TFRAMES=$(mktemp -d); TGIF=$(mktemp -d)
node render-frames.cjs "$PWD/terminal.html" "$TFRAMES" 30
ffmpeg -y -loglevel error -framerate 30 -i "$TFRAMES/f%04d.png" -c:v libx264 -pix_fmt yuv420p -crf 18 -preset slow -movflags +faststart out/terminal-demo.mp4
node render-frames.cjs "$PWD/terminal.html" "$TGIF" 20 stillbg
ffmpeg -y -loglevel error -framerate 20 -i "$TGIF/f%04d.png" -vf "fps=20,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle" -loop 0 out/terminal-demo.gif
rm -rf "$FRAMES" "$GIFFRAMES" "$CARDS" "$TFRAMES" "$TGIF"
