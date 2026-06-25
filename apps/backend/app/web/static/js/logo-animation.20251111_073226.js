/**
 * Retro Wireframe Sphere Logo Animation - v0.2
 * Three.js animated logo with Saturn-style particle rings
 */

import * as THREE from '/static/js/vendor/three.module.js';

export function initLogo(containerId, initialConfig = {}, callbacks = {}) {
    const container = document.getElementById(containerId);
    if (!container) {
        console.error(`Container ${containerId} not found`);
        return;
    }

    // Optional callback for color changes (called every frame)
    const onColorUpdate = callbacks.onColorUpdate || null;

    // Configuration with defaults
    const config = {
        // Sphere
        vertices: initialConfig.vertices || 28,
        sphereSize: initialConfig.sphereSize || 1.7,
        rotationSpeed: initialConfig.rotationSpeed || 0.001,
        pulseAmount: initialConfig.pulseAmount || 0.03,
        pulseSpeed: initialConfig.pulseSpeed || 2.7,

        // Opacity/Visibility
        wireframeOpacity: initialConfig.wireframeOpacity || 0.9,
        backsideOpacity: initialConfig.backsideOpacity || 0.4,
        glowIntensity: initialConfig.glowIntensity || 0.25,

        // Colors - now with gradient support
        primaryColor: initialConfig.primaryColor || '#ff3300',
        primaryGradientColor: initialConfig.primaryGradientColor || '#ff6633',
        useGradient: initialConfig.useGradient || false,
        gradientWaveSpeed: initialConfig.gradientWaveSpeed || 0.5, // Speed of gradient wave across sphere
        secondaryColor: initialConfig.secondaryColor || '#ff6633',
        glowColor: initialConfig.glowColor || '#ff3300',

        // Rings
        ringCount: initialConfig.ringCount || 3,
        ringParticleCount: initialConfig.ringParticleCount || 120,
        ringParticleSize: initialConfig.ringParticleSize || 0.02,
        ringRadius: initialConfig.ringRadius || 2.5,
        ringRotationSpeed: initialConfig.ringRotationSpeed || 0.3,
        ringPulseAmount: initialConfig.ringPulseAmount || 0.15,
        ringPulseSpeed: initialConfig.ringPulseSpeed || 2.0,
        ringRadiusPulse: initialConfig.ringRadiusPulse || 0.1, // How much the ring radius expands/contracts
        ringRadiusPulseSpeed: initialConfig.ringRadiusPulseSpeed || 1.5,

        // Particle lighting effects
        lightPattern: initialConfig.lightPattern || 'wave', // 'wave', 'chase', 'sparkle', 'pulse', 'trail'
        lightSpeed: initialConfig.lightSpeed || 0.5,
        trailLength: initialConfig.trailLength || 50, // 0-100% of ring
        fadeCurve: initialConfig.fadeCurve || 'linear', // 'linear', 'sqrt', 'squared', 'exponential'

        // Camera
        cameraDistance: initialConfig.cameraDistance || 5.0,
        cameraOrbitSpeed: initialConfig.cameraOrbitSpeed || 0.3,
    };

    // Scene setup
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, 1, 0.1, 1000);
    camera.position.z = config.cameraDistance;

    const renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true
    });
    renderer.setSize(600, 600);
    renderer.setClearColor(0x000000, 0);
    container.appendChild(renderer.domElement);

    // Create complete wireframe sphere (all lines)
    let sphereGeometry = new THREE.SphereGeometry(config.sphereSize, config.vertices, config.vertices);
    const wireframeMaterial = new THREE.MeshBasicMaterial({
        color: new THREE.Color(config.primaryColor),
        wireframe: true,
        transparent: true,
        opacity: config.wireframeOpacity
    });
    const sphere = new THREE.Mesh(sphereGeometry, wireframeMaterial);
    scene.add(sphere);

    // Solid blocking surface - blocks back lines when opaque
    let blockGeometry = new THREE.SphereGeometry(config.sphereSize * 0.99, 32, 32);
    const blockMaterial = new THREE.MeshBasicMaterial({
        color: new THREE.Color('#000000'),
        transparent: true,
        opacity: 1 - config.backsideOpacity,
        side: THREE.FrontSide
    });
    const blockSphere = new THREE.Mesh(blockGeometry, blockMaterial);
    scene.add(blockSphere);

    // Inner glow
    let glowGeometry = new THREE.SphereGeometry(config.sphereSize * 0.93, 32, 32);
    const glowMaterial = new THREE.MeshBasicMaterial({
        color: new THREE.Color(config.glowColor),
        transparent: true,
        opacity: config.glowIntensity,
        side: THREE.BackSide
    });
    const glow = new THREE.Mesh(glowGeometry, glowMaterial);
    scene.add(glow);

    // Particle Rings System
    let rings = [];

    function createRings() {
        // Remove old rings
        rings.forEach(ring => {
            ring.particles.forEach(p => scene.remove(p.mesh));
        });
        rings = [];

        // Create multiple rings at different angles
        for (let i = 0; i < config.ringCount; i++) {
            const ring = {
                particles: [],
                angle: (i / config.ringCount) * Math.PI, // Vary angle
                tilt: Math.random() * Math.PI * 0.5, // Random tilt
                radius: config.ringRadius + (i * 0.3),
                rotationOffset: Math.random() * Math.PI * 2,
                sparkleOffset: Math.random() * 1000 // Random offset for sparkle pattern
            };

            // Create particles around the ring
            for (let j = 0; j < config.ringParticleCount; j++) {
                const particleGeometry = new THREE.SphereGeometry(config.ringParticleSize, 8, 8);
                const particleMaterial = new THREE.MeshBasicMaterial({
                    color: new THREE.Color(config.secondaryColor),
                    transparent: true,
                    opacity: 0.6
                });
                const particle = new THREE.Mesh(particleGeometry, particleMaterial);
                scene.add(particle);

                ring.particles.push({
                    mesh: particle,
                    angleOffset: (j / config.ringParticleCount) * Math.PI * 2,
                    index: j // Store index for pattern calculations
                });
            }

            rings.push(ring);
        }
    }

    createRings();

    // Animation
    let time = 0;
    function animate() {
        requestAnimationFrame(animate);
        time += 0.01;

        // Sphere rotation
        sphere.rotation.x += config.rotationSpeed;
        sphere.rotation.y += config.rotationSpeed * 1.5;
        blockSphere.rotation.x += config.rotationSpeed;
        blockSphere.rotation.y += config.rotationSpeed * 1.5;

        // Camera orbit
        const radius = config.cameraDistance;
        camera.position.x = Math.cos(time * config.cameraOrbitSpeed) * radius;
        camera.position.z = Math.sin(time * config.cameraOrbitSpeed) * radius;
        camera.position.y = Math.sin(time * config.cameraOrbitSpeed * 0.67) * 1.5;
        camera.lookAt(0, 0, 0);

        // Pulse effect
        const pulse = 1 + Math.sin(time * config.pulseSpeed) * config.pulseAmount;
        sphere.scale.setScalar(pulse);
        blockSphere.scale.setScalar(pulse);

        // Glow pulse
        glow.scale.setScalar(pulse * 1.02);
        glow.material.opacity = config.glowIntensity * (0.8 + Math.sin(time * config.pulseSpeed) * 0.3);

        // Wireframe brightness pulse
        wireframeMaterial.opacity = config.wireframeOpacity * (0.9 + Math.sin(time * config.pulseSpeed) * 0.15);

        // Block sphere opacity (inverse of backsideOpacity)
        blockMaterial.opacity = (1 - config.backsideOpacity) * 0.95;

        // Update gradient color if enabled - wave travels across sphere
        let currentColor = config.primaryColor;
        if (config.useGradient) {
            // Create wave based on sphere rotation and time
            // This creates a gradient that travels around the Y axis
            const wavePosition = (sphere.rotation.y + time * config.gradientWaveSpeed) % (Math.PI * 2);
            const gradientProgress = (Math.sin(wavePosition) + 1) / 2; // 0 to 1

            const colorStart = new THREE.Color(config.primaryColor);
            const colorEnd = new THREE.Color(config.primaryGradientColor);
            const blendedColor = new THREE.Color().lerpColors(colorStart, colorEnd, gradientProgress);

            wireframeMaterial.color.set(blendedColor);

            // Convert to hex for callback
            currentColor = '#' + blendedColor.getHexString();
        }

        // Notify callback of current color (for CSS glow sync)
        if (onColorUpdate) {
            onColorUpdate(currentColor);
        }

        // Animate rings with light patterns
        rings.forEach((ring, ringIndex) => {
            const ringTime = time * config.ringRotationSpeed;

            // Calculate ring-specific pulse (for particle size)
            const ringPulse = 1 + Math.sin(time * config.ringPulseSpeed) * config.ringPulseAmount;

            // Calculate radius pulse (makes the ring expand/contract)
            const radiusPulse = 1 + Math.sin(time * config.ringRadiusPulseSpeed) * config.ringRadiusPulse;
            const pulsedRadius = ring.radius * radiusPulse;

            ring.particles.forEach((particle, particleIndex) => {
                const angle = particle.angleOffset + ringTime + ring.rotationOffset;

                // Position on ring (using pulsed radius)
                let x = Math.cos(angle) * pulsedRadius;
                let z = Math.sin(angle) * pulsedRadius;
                let y = 0;

                // Apply ring tilt and rotation
                const cosAngle = Math.cos(ring.angle);
                const sinAngle = Math.sin(ring.angle);
                const cosTilt = Math.cos(ring.tilt);
                const sinTilt = Math.sin(ring.tilt);

                // Rotate around Y axis first
                const x1 = x * cosAngle - z * sinAngle;
                const z1 = x * sinAngle + z * cosAngle;

                // Then tilt
                const y1 = y * cosTilt - z1 * sinTilt;
                const z2 = y * sinTilt + z1 * cosTilt;

                particle.mesh.position.set(x1, y1, z2);

                // Apply pulse to particle size (independent from sphere)
                particle.mesh.scale.setScalar(ringPulse);

                // Calculate brightness based on light pattern
                const normalizedIndex = particle.index / config.ringParticleCount; // 0 to 1
                const lightTime = time * config.lightSpeed;
                const trailLengthNormalized = config.trailLength / 100;

                let patternIntensity = 1.0; // Base pattern brightness (0-1)
                let trailFade = 0.0; // Trail gradient multiplier (0-1)

                // First, calculate where the "hot spot" is based on pattern
                let hotSpotPosition = (lightTime % 1);

                // Calculate distance BEHIND hot spot (trail follows behind)
                // Reverse the calculation so trail goes backwards from hot spot
                let distanceFromHotSpot = hotSpotPosition - normalizedIndex;
                if (distanceFromHotSpot < 0) distanceFromHotSpot += 1;

                // Calculate trail fade: 1.0 at hot spot, fades smoothly over trail length
                if (trailLengthNormalized > 0) {
                    const normalizedDistance = distanceFromHotSpot / trailLengthNormalized;

                    if (normalizedDistance <= 1.0) {
                        // Linear fade from 1.0 to 0.0
                        const linearFade = 1.0 - normalizedDistance;

                        // Apply different fade curves
                        switch (config.fadeCurve) {
                            case 'linear':
                                trailFade = linearFade;
                                break;
                            case 'sqrt':
                                // Slower fade at start, faster at end
                                trailFade = Math.sqrt(linearFade);
                                break;
                            case 'squared':
                                // Faster fade at start, slower at end
                                trailFade = linearFade * linearFade;
                                break;
                            case 'exponential':
                                // Natural exponential decay
                                trailFade = Math.exp(-3 * (1 - linearFade));
                                break;
                            default:
                                trailFade = linearFade;
                        }
                    } else {
                        trailFade = 0.0;
                    }
                } else {
                    // No trail - only show the hot spot
                    trailFade = distanceFromHotSpot < 0.01 ? 1.0 : 0.0;
                }

                // Now calculate pattern-specific intensity modifications
                switch (config.lightPattern) {
                    case 'wave':
                        // Sine wave - modulates the trail brightness
                        const waveValue = (Math.sin(normalizedIndex * Math.PI * 4 + lightTime * 2) + 1) / 2;
                        patternIntensity = waveValue;
                        break;

                    case 'chase':
                        // Broadway chase - hard sections that light up
                        const chaseWidth = 0.15;
                        const wrappedDistance = Math.min(distanceFromHotSpot, 1 - distanceFromHotSpot);
                        patternIntensity = wrappedDistance < chaseWidth ? 1.0 : 0.0;
                        break;

                    case 'sparkle':
                        // Random sparkles - modulates trail brightness
                        const sparkleValue = Math.sin(particle.index * 0.1 + time * 2 + ring.sparkleOffset) *
                                            Math.sin(particle.index * 0.3 + time * 3 + ring.sparkleOffset * 2);
                        patternIntensity = sparkleValue > 0.7 ? 1.0 : 0.2;
                        break;

                    case 'pulse':
                        // All particles pulse together - no trail effect, just pulse
                        patternIntensity = (Math.sin(lightTime * 3) + 1) / 2 * 0.7 + 0.3;
                        trailFade = 1.0; // Override trail - show everywhere
                        break;

                    case 'trail':
                        // Pure gradient trail - trail fade IS the pattern
                        patternIntensity = 1.0;
                        break;

                    default:
                        patternIntensity = 1.0;
                }

                // Combine pattern intensity with trail fade
                let brightness = patternIntensity * trailFade;

                // Base opacity with depth effect
                const distance = particle.mesh.position.distanceTo(camera.position);
                const depthFade = Math.min(1.0, 0.3 + (1 / distance) * 2);

                particle.mesh.material.opacity = brightness * depthFade;
            });
        });

        renderer.render(scene, camera);
    }

    animate();

    // Return controller object for live updates
    return {
        updateConfig(updates) {
            Object.assign(config, updates);

            // Update sphere geometry if vertices changed
            if (updates.vertices !== undefined) {
                scene.remove(sphere);
                sphereGeometry.dispose();

                sphereGeometry = new THREE.SphereGeometry(config.sphereSize, config.vertices, config.vertices);
                sphere.geometry = sphereGeometry;

                scene.add(sphere);
            }

            // Update sphere size
            if (updates.sphereSize !== undefined) {
                scene.remove(sphere);
                scene.remove(blockSphere);
                scene.remove(glow);
                sphereGeometry.dispose();
                blockGeometry.dispose();
                glowGeometry.dispose();

                sphereGeometry = new THREE.SphereGeometry(config.sphereSize, config.vertices, config.vertices);
                sphere.geometry = sphereGeometry;

                blockGeometry = new THREE.SphereGeometry(config.sphereSize * 0.99, 32, 32);
                blockSphere.geometry = blockGeometry;

                glowGeometry = new THREE.SphereGeometry(config.sphereSize * 0.93, 32, 32);
                glow.geometry = glowGeometry;

                scene.add(sphere);
                scene.add(blockSphere);
                scene.add(glow);
            }

            // Update colors
            if (updates.primaryColor !== undefined && !config.useGradient) {
                wireframeMaterial.color.set(config.primaryColor);
            }

            if (updates.glowColor !== undefined) {
                glowMaterial.color.set(config.glowColor);
            }

            if (updates.secondaryColor !== undefined) {
                rings.forEach(ring => {
                    ring.particles.forEach(p => {
                        p.mesh.material.color.set(config.secondaryColor);
                    });
                });
            }

            // Update gradient (turn gradient on/off or change gradient color)
            if (updates.useGradient !== undefined || updates.primaryGradientColor !== undefined) {
                if (!config.useGradient) {
                    // Reset to primary color when gradient is turned off
                    wireframeMaterial.color.set(config.primaryColor);
                }
            }

            // Recreate rings if parameters changed
            if (updates.ringCount !== undefined ||
                updates.ringParticleCount !== undefined ||
                updates.ringParticleSize !== undefined ||
                updates.ringRadius !== undefined) {
                createRings();
            }

            // Light pattern changes take effect immediately (no recreation needed)
        },

        getConfig() {
            return { ...config };
        }
    };
}
