/**
 * Login Page - Animated Logo Initialization
 */

import { initLogo } from '/static/js/logo-animation.js';

// Login page preset configuration
const loginPreset = {
    canvasWidth: 1400,
    canvasHeight: 1200,
    vertices: 28,
    sphereSize: 1.7,
    rotationSpeed: 0.001,
    pulseAmount: 0.05,
    pulseSpeed: 1.8,
    wireframeOpacity: 0.9,
    backsideOpacity: 0.4,
    glowIntensity: 0.25,
    primaryColor: "#ff3300",
    primaryGradientColor: "#ffffff",
    useGradient: true,
    gradientWaveSpeed: 0.5,
    secondaryColor: "#5e668d",
    glowColor: "#ffffff",
    ringCount: 3,
    ringParticleCount: 280,
    ringParticleSize: 0.01,
    ringRadius: 2.8,
    ringRotationSpeed: 0.3,
    ringPulseAmount: 0.15,
    ringPulseSpeed: 2,
    ringRadiusPulse: 0.09,
    ringRadiusPulseSpeed: 1.6,
    lightPattern: "trail",
    lightSpeed: 0.5,
    trailLength: 50,
    fadeCurve: "linear",
    cameraDistance: 8.0,
    cameraOrbitSpeed: 0.05
};

// CSS Glow settings
let cssGlowColor = "#ffe4e3";
let cssGlowIntensity = 40;
let cssGlowLocked = true;
let lastCssGlowColor = cssGlowColor;

function updateCSSGlow() {
    const container = document.getElementById('canvas-container');
    if (cssGlowIntensity === 0) {
        container.style.filter = 'none';
    } else {
        const r = parseInt(cssGlowColor.substr(1, 2), 16);
        const g = parseInt(cssGlowColor.substr(3, 2), 16);
        const b = parseInt(cssGlowColor.substr(5, 2), 16);
        container.style.filter = `drop-shadow(0 0 ${cssGlowIntensity}px rgba(${r}, ${g}, ${b}, 0.6))`;
    }
}

// Initialize logo with preset and CSS glow callback
const logoController = initLogo('canvas-container', loginPreset, {
    onColorUpdate: (currentColor) => {
        if (cssGlowLocked && currentColor !== lastCssGlowColor) {
            cssGlowColor = currentColor;
            lastCssGlowColor = currentColor;
            updateCSSGlow();
        }
    }
});

// Initialize CSS glow
updateCSSGlow();

// TODO: Add Easter egg keyboard combo to show controls later
// Example: Konami code or Ctrl+Shift+L

// Canvas size: 1400x1200 (configurable via canvasWidth/canvasHeight)
// Position: Moved up 200px from center for better composition with login form
