/**
 * Logo showcase controls (/settings/logo-demo).
 *
 * A design sandbox: sliders and colour pickers drive the animated logo's CSS
 * variables live, so the look can be tuned without a rebuild.
 */

import { initLogo } from '/static/js/logo-animation.js';

// CSS Glow variables
let cssGlowColor = '#ff3300';
let cssGlowIntensity = 40;
let cssGlowLocked = true;
let lastCssGlowColor = cssGlowColor;

function updateCSSGlow() {
    const container = document.getElementById('canvas-container');
    if (cssGlowIntensity === 0) {
        container.style.filter = 'none';
    } else {
        // Convert hex color to rgba
        const r = parseInt(cssGlowColor.substr(1, 2), 16);
        const g = parseInt(cssGlowColor.substr(3, 2), 16);
        const b = parseInt(cssGlowColor.substr(5, 2), 16);
        container.style.filter = `drop-shadow(0 0 ${cssGlowIntensity}px rgba(${r}, ${g}, ${b}, 0.6))`;
    }
}

// Initialize logo with color update callback
const logoController = initLogo('canvas-container', {}, {
    onColorUpdate: (currentColor) => {
        // Update CSS glow if locked (only if color changed to reduce DOM updates)
        if (cssGlowLocked && currentColor !== lastCssGlowColor) {
            cssGlowColor = currentColor;
            lastCssGlowColor = currentColor;
            updateCSSGlow();
        }
    }
});

// Initialize CSS glow
updateCSSGlow();

// CSS Glow lock to sphere color logic
const primaryColorPicker = document.getElementById('primaryColor');
const cssGlowColorPicker = document.getElementById('cssGlowColor');
const lockCssGlowCheckbox = document.getElementById('lockCssGlowToSphere');

function updateCssGlowLock() {
    if (cssGlowLocked) {
        cssGlowColorPicker.disabled = true;
        cssGlowColorPicker.style.opacity = '0.5';
        cssGlowColorPicker.style.cursor = 'not-allowed';
    } else {
        cssGlowColorPicker.disabled = false;
        cssGlowColorPicker.style.opacity = '1';
        cssGlowColorPicker.style.cursor = 'pointer';
    }
}

// Initialize CSS glow lock state
updateCssGlowLock();

lockCssGlowCheckbox.addEventListener('change', (e) => {
    cssGlowLocked = e.target.checked;
    updateCssGlowLock();
    if (cssGlowLocked) {
        // Sync CSS glow color to primary color immediately
        const currentPrimaryColor = primaryColorPicker.value;
        cssGlowColorPicker.value = currentPrimaryColor;
        cssGlowColor = currentPrimaryColor;
        updateCSSGlow();
    }
});

// Setup all controls
const controls = [
    'vertices', 'sphereSize', 'rotationSpeed', 'pulseAmount', 'pulseSpeed',
    'wireframeOpacity', 'backsideOpacity', 'glowIntensity',
    'primaryColor', 'glowColor', 'primaryGradientColor', 'useGradient', 'gradientWaveSpeed', 'secondaryColor',
    'ringCount', 'ringParticleCount', 'ringParticleSize', 'ringRadius', 'ringRotationSpeed',
    'ringPulseAmount', 'ringPulseSpeed', 'ringRadiusPulse', 'ringRadiusPulseSpeed',
    'lightSpeed', 'trailLength',
    'cameraDistance', 'cameraOrbitSpeed'
];

controls.forEach(controlId => {
    const input = document.getElementById(controlId);
    const valueDisplay = document.getElementById(`${controlId}-value`);

    if (input && input.type !== 'checkbox' && input.type !== 'color') {
        // Range sliders
        input.addEventListener('input', (e) => {
            const value = parseFloat(e.target.value);
            if (valueDisplay) {
                valueDisplay.textContent = value;
            }
            logoController.updateConfig({ [controlId]: value });
        });
    } else if (input && input.type === 'color') {
        // Color pickers
        input.addEventListener('input', (e) => {
            logoController.updateConfig({ [controlId]: e.target.value });

            // If primary color changes and CSS glow is locked, update CSS glow too
            if (controlId === 'primaryColor' && cssGlowLocked) {
                cssGlowColorPicker.value = e.target.value;
                cssGlowColor = e.target.value;
                updateCSSGlow();
            }
        });
    } else if (input && input.type === 'checkbox') {
        // Checkboxes
        input.addEventListener('change', (e) => {
            logoController.updateConfig({ [controlId]: e.target.checked });
        });
    }
});

// Handle select dropdowns
const lightPatternSelect = document.getElementById('lightPattern');
if (lightPatternSelect) {
    lightPatternSelect.addEventListener('change', (e) => {
        logoController.updateConfig({ lightPattern: e.target.value });
    });
}

const fadeCurveSelect = document.getElementById('fadeCurve');
if (fadeCurveSelect) {
    fadeCurveSelect.addEventListener('change', (e) => {
        logoController.updateConfig({ fadeCurve: e.target.value });
    });
}

// CSS Glow color picker (manual control when unlocked)
if (cssGlowColorPicker) {
    cssGlowColorPicker.addEventListener('input', (e) => {
        if (!cssGlowLocked) {
            cssGlowColor = e.target.value;
            updateCSSGlow();
        }
    });
}

// CSS Glow intensity slider
const cssGlowIntensitySlider = document.getElementById('cssGlowIntensity');
const cssGlowIntensityValue = document.getElementById('cssGlowIntensity-value');
if (cssGlowIntensitySlider) {
    cssGlowIntensitySlider.addEventListener('input', (e) => {
        cssGlowIntensity = parseInt(e.target.value);
        if (cssGlowIntensityValue) {
            cssGlowIntensityValue.textContent = cssGlowIntensity;
        }
        updateCSSGlow();
    });
}

// ========== PRESET SYSTEM ==========

// Preset configurations
const presets = {
    default: {
        vertices: 28, sphereSize: 1.7, rotationSpeed: 0.001, pulseAmount: 0.03, pulseSpeed: 2.7,
        wireframeOpacity: 0.9, backsideOpacity: 0.4, glowIntensity: 0.25,
        primaryColor: '#ff3300', glowColor: '#ff3300', useGradient: false,
        primaryGradientColor: '#ff6633', gradientWaveSpeed: 0.5, secondaryColor: '#ff6633',
        ringCount: 3, ringParticleCount: 120, ringParticleSize: 0.02, ringRadius: 2.5,
        ringRotationSpeed: 0.3, ringPulseAmount: 0.15, ringPulseSpeed: 2.0,
        ringRadiusPulse: 0.1, ringRadiusPulseSpeed: 1.5,
        lightPattern: 'wave', lightSpeed: 0.5, trailLength: 50, fadeCurve: 'linear',
        cameraDistance: 5.0, cameraOrbitSpeed: 0.3,
        cssGlowColor: '#ff3300', cssGlowIntensity: 40, cssGlowLocked: true
    },
    minimal: {
        vertices: 16, sphereSize: 1.5, rotationSpeed: 0.0005, pulseAmount: 0.01, pulseSpeed: 1.5,
        wireframeOpacity: 0.7, backsideOpacity: 0.2, glowIntensity: 0.15,
        primaryColor: '#3366ff', glowColor: '#3366ff', useGradient: false,
        primaryGradientColor: '#6699ff', gradientWaveSpeed: 0.3, secondaryColor: '#6699ff',
        ringCount: 1, ringParticleCount: 60, ringParticleSize: 0.015, ringRadius: 2.2,
        ringRotationSpeed: 0.2, ringPulseAmount: 0.05, ringPulseSpeed: 1.0,
        ringRadiusPulse: 0.05, ringRadiusPulseSpeed: 1.0,
        lightPattern: 'trail', lightSpeed: 0.3, trailLength: 30, fadeCurve: 'linear',
        cameraDistance: 6.0, cameraOrbitSpeed: 0.2,
        cssGlowColor: '#3366ff', cssGlowIntensity: 20, cssGlowLocked: true
    },
    intense: {
        vertices: 40, sphereSize: 2.0, rotationSpeed: 0.003, pulseAmount: 0.08, pulseSpeed: 4.0,
        wireframeOpacity: 1.0, backsideOpacity: 0.6, glowIntensity: 0.4,
        primaryColor: '#ff0066', glowColor: '#ff0066', useGradient: true,
        primaryGradientColor: '#ffff00', gradientWaveSpeed: 1.5, secondaryColor: '#00ffff',
        ringCount: 5, ringParticleCount: 200, ringParticleSize: 0.03, ringRadius: 2.8,
        ringRotationSpeed: 0.6, ringPulseAmount: 0.25, ringPulseSpeed: 3.0,
        ringRadiusPulse: 0.2, ringRadiusPulseSpeed: 2.5,
        lightPattern: 'chase', lightSpeed: 1.5, trailLength: 80, fadeCurve: 'exponential',
        cameraDistance: 4.5, cameraOrbitSpeed: 0.5,
        cssGlowColor: '#ff0066', cssGlowIntensity: 60, cssGlowLocked: true
    }
};

// Function to update all UI controls from config
function updateUIFromConfig(config) {
    Object.keys(config).forEach(key => {
        const input = document.getElementById(key);
        const valueDisplay = document.getElementById(`${key}-value`);

        if (input) {
            if (input.type === 'checkbox') {
                input.checked = config[key];
            } else if (input.type === 'color') {
                input.value = config[key];
            } else if (input.tagName === 'SELECT') {
                input.value = config[key];
            } else {
                input.value = config[key];
                if (valueDisplay) {
                    valueDisplay.textContent = config[key];
                }
            }
        }

        // Handle CSS glow variables
        if (key === 'cssGlowColor') {
            cssGlowColor = config[key];
        } else if (key === 'cssGlowIntensity') {
            cssGlowIntensity = config[key];
        } else if (key === 'cssGlowLocked') {
            cssGlowLocked = config[key];
            updateCssGlowLock();
        }
    });

    // Update logo controller
    logoController.updateConfig(config);
    updateCSSGlow();
}

// Preset buttons
document.getElementById('preset-default')?.addEventListener('click', () => {
    updateUIFromConfig(presets.default);
});

document.getElementById('preset-minimal')?.addEventListener('click', () => {
    updateUIFromConfig(presets.minimal);
});

document.getElementById('preset-intense')?.addEventListener('click', () => {
    updateUIFromConfig(presets.intense);
});

// Export config
document.getElementById('export-config')?.addEventListener('click', () => {
    const currentConfig = logoController.getConfig();
    // Add CSS glow settings
    currentConfig.cssGlowColor = cssGlowColor;
    currentConfig.cssGlowIntensity = cssGlowIntensity;
    currentConfig.cssGlowLocked = cssGlowLocked;

    const configJSON = JSON.stringify(currentConfig, null, 2);
    const textarea = document.getElementById('config-textarea');
    textarea.style.display = 'block';
    textarea.value = configJSON;
    textarea.select();

    // Copy to clipboard
    navigator.clipboard.writeText(configJSON).then(() => {
        const btn = document.getElementById('export-config');
        const originalText = btn.textContent;
        btn.textContent = '✅ Copied!';
        setTimeout(() => {
            btn.textContent = originalText;
            textarea.style.display = 'none';
        }, 2000);
    });
});

// Import config
document.getElementById('import-config')?.addEventListener('click', () => {
    const textarea = document.getElementById('config-textarea');
    if (textarea.style.display === 'none' || textarea.style.display === '') {
        textarea.style.display = 'block';
        textarea.focus();
        document.getElementById('import-config').textContent = '✔️ Apply Config';
    } else {
        try {
            const config = JSON.parse(textarea.value);
            updateUIFromConfig(config);
            textarea.style.display = 'none';
            document.getElementById('import-config').textContent = '📥 Import Config';

            // Show success feedback
            const btn = document.getElementById('import-config');
            const originalText = btn.textContent;
            btn.textContent = '✅ Applied!';
            btn.style.background = 'rgba(34, 197, 94, 0.3)';
            btn.style.borderColor = 'rgba(34, 197, 94, 0.5)';
            btn.style.color = '#4ade80';
            setTimeout(() => {
                btn.textContent = '📥 Import Config';
                btn.style.background = 'rgba(59, 130, 246, 0.3)';
                btn.style.borderColor = 'rgba(59, 130, 246, 0.5)';
                btn.style.color = '#60a5fa';
            }, 2000);
        } catch (e) {
            window.showToast?.('Invalid JSON config: ' + e.message, 'error');
        }
    }
});
