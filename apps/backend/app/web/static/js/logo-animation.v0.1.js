/**
 * Retro Wireframe Sphere Logo Animation
 * Three.js animated logo for LuxSwirl monitoring platform
 */

import * as THREE from '/static/js/vendor/three.module.js';

export function initLogo(containerId, initialConfig = {}) {
    const container = document.getElementById(containerId);
    if (!container) {
        console.error(`Container ${containerId} not found`);
        return;
    }

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

        // Colors
        primaryColor: initialConfig.primaryColor || '#ff3300',
        secondaryColor: initialConfig.secondaryColor || '#ff6633',
        useGradient: initialConfig.useGradient || false,
        gradientColor: initialConfig.gradientColor || '#ff9900',

        // Comets
        cometCount: initialConfig.cometCount || 5,
        cometSpeed: initialConfig.cometSpeed || 0.5,
        cometSize: initialConfig.cometSize || 0.08,
        trailLength: initialConfig.trailLength || 100,

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
    // Opacity is INVERSE of backsideOpacity (0 = see through back lines, 1 = block back lines)
    let blockGeometry = new THREE.SphereGeometry(config.sphereSize * 0.99, 32, 32);
    const blockMaterial = new THREE.MeshBasicMaterial({
        color: new THREE.Color('#000000'), // Black to block
        transparent: true,
        opacity: 1 - config.backsideOpacity, // Inverse!
        side: THREE.FrontSide
    });
    const blockSphere = new THREE.Mesh(blockGeometry, blockMaterial);
    scene.add(blockSphere);

    // Inner glow
    let glowGeometry = new THREE.SphereGeometry(config.sphereSize * 0.93, 32, 32);
    const glowMaterial = new THREE.MeshBasicMaterial({
        color: new THREE.Color(config.primaryColor),
        transparent: true,
        opacity: config.glowIntensity,
        side: THREE.BackSide
    });
    const glow = new THREE.Mesh(glowGeometry, glowMaterial);
    scene.add(glow);

    // Comets
    let comets = [];

    function createComets(count) {
        // Remove old comets
        comets.forEach(c => {
            scene.remove(c.mesh);
            scene.remove(c.trail);
        });
        comets = [];

        for (let i = 0; i < count; i++) {
            // Comet head
            const cometGeometry = new THREE.SphereGeometry(config.cometSize, 8, 8);
            const cometMaterial = new THREE.MeshBasicMaterial({
                color: new THREE.Color(config.secondaryColor),
                transparent: true,
                opacity: 1
            });
            const comet = new THREE.Mesh(cometGeometry, cometMaterial);

            // Comet glow
            const cometGlowGeometry = new THREE.SphereGeometry(config.cometSize * 1.875, 8, 8);
            const cometGlowMaterial = new THREE.MeshBasicMaterial({
                color: new THREE.Color(config.primaryColor),
                transparent: true,
                opacity: 0.4,
                blending: THREE.AdditiveBlending
            });
            const cometGlow = new THREE.Mesh(cometGlowGeometry, cometGlowMaterial);
            comet.add(cometGlow);

            // Trail
            const trailGeometry = new THREE.BufferGeometry();
            const trailPositions = new Float32Array(config.trailLength * 3);
            const trailColors = new Float32Array(config.trailLength * 3);

            trailGeometry.setAttribute('position', new THREE.BufferAttribute(trailPositions, 3));
            trailGeometry.setAttribute('color', new THREE.BufferAttribute(trailColors, 3));

            const trailMaterial = new THREE.LineBasicMaterial({
                vertexColors: true,
                transparent: true,
                opacity: 1,
                blending: THREE.AdditiveBlending
            });

            const trail = new THREE.Line(trailGeometry, trailMaterial);
            scene.add(trail);
            scene.add(comet);

            comets.push({
                mesh: comet,
                glow: cometGlow,
                trail: trail,
                trailHistory: [],
                radius: 2.5 + Math.random() * 1.2,
                speed: 0.3 + Math.random() * 0.4,
                inclinationAngle: Math.random() * Math.PI * 2,
                orbitTilt: Math.random() * Math.PI,
                offset: (i / count) * Math.PI * 2
            });
        }
    }

    createComets(config.cometCount);

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

        // Animate comets
        comets.forEach((comet) => {
            const angle = time * comet.speed * config.cometSpeed + comet.offset;

            // 3D orbit calculation
            let x = Math.cos(angle) * comet.radius;
            let y = Math.sin(angle) * comet.radius;
            let z = 0;

            const cosInc = Math.cos(comet.inclinationAngle);
            const sinInc = Math.sin(comet.inclinationAngle);
            const cosTilt = Math.cos(comet.orbitTilt);
            const sinTilt = Math.sin(comet.orbitTilt);

            const x1 = x * cosInc - z * sinInc;
            const z1 = x * sinInc + z * cosInc;
            const y1 = y * cosTilt - z1 * sinTilt;
            const z2 = y * sinTilt + z1 * cosTilt;

            comet.mesh.position.set(x1, y1, z2);

            // Trail history
            comet.trailHistory.push({ x: x1, y: y1, z: z2 });
            if (comet.trailHistory.length > config.trailLength) {
                comet.trailHistory.shift();
            }

            // Update trail geometry
            const positions = comet.trail.geometry.attributes.position.array;
            const colors = comet.trail.geometry.attributes.color.array;

            // Color for gradient
            const primaryRGB = new THREE.Color(config.primaryColor);
            const secondaryRGB = new THREE.Color(config.useGradient ? config.gradientColor : config.secondaryColor);

            for (let i = 0; i < comet.trailHistory.length; i++) {
                const pos = comet.trailHistory[i];
                positions[i * 3] = pos.x;
                positions[i * 3 + 1] = pos.y;
                positions[i * 3 + 2] = pos.z;

                const fade = i / comet.trailHistory.length;
                const color = new THREE.Color().lerpColors(primaryRGB, secondaryRGB, fade);

                colors[i * 3] = color.r * fade;
                colors[i * 3 + 1] = color.g * fade;
                colors[i * 3 + 2] = color.b * fade;
            }

            // Fill remaining positions
            if (comet.trailHistory.length > 0) {
                const lastPos = comet.trailHistory[comet.trailHistory.length - 1];
                for (let i = comet.trailHistory.length; i < config.trailLength; i++) {
                    positions[i * 3] = lastPos.x;
                    positions[i * 3 + 1] = lastPos.y;
                    positions[i * 3 + 2] = lastPos.z;
                    colors[i * 3] = 0;
                    colors[i * 3 + 1] = 0;
                    colors[i * 3 + 2] = 0;
                }
            }

            comet.trail.geometry.attributes.position.needsUpdate = true;
            comet.trail.geometry.attributes.color.needsUpdate = true;
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
            if (updates.primaryColor !== undefined) {
                wireframeMaterial.color.set(config.primaryColor);
                glowMaterial.color.set(config.primaryColor);
            }

            if (updates.secondaryColor !== undefined) {
                comets.forEach(c => {
                    c.mesh.material.color.set(config.secondaryColor);
                });
            }

            // Update comet count
            if (updates.cometCount !== undefined) {
                createComets(config.cometCount);
            }

            // Update comet size
            if (updates.cometSize !== undefined) {
                comets.forEach(c => {
                    c.mesh.geometry.dispose();
                    c.glow.geometry.dispose();
                    c.mesh.geometry = new THREE.SphereGeometry(config.cometSize, 8, 8);
                    c.glow.geometry = new THREE.SphereGeometry(config.cometSize * 1.875, 8, 8);
                });
            }

            // Update trail length
            if (updates.trailLength !== undefined) {
                comets.forEach(c => {
                    const trailPositions = new Float32Array(config.trailLength * 3);
                    const trailColors = new Float32Array(config.trailLength * 3);
                    c.trail.geometry.setAttribute('position', new THREE.BufferAttribute(trailPositions, 3));
                    c.trail.geometry.setAttribute('color', new THREE.BufferAttribute(trailColors, 3));
                    c.trailHistory = [];
                });
            }
        },

        getConfig() {
            return { ...config };
        }
    };
}
