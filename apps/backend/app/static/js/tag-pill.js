/**
 * A removable tag pill, built with DOM calls: a tag comes from a stored value or a
 * URL, so it is set as text and as a data attribute and never parsed as HTML.
 */

const REMOVE_ICON = `
    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
    </svg>
`;

/**
 * @param {string} tag
 * @param {object} options
 * @param {string} options.pillClass the pill's classes
 * @param {string} options.buttonClass the remove button's classes
 * @param {string} options.dataKey the dataset key the remove button carries the tag in
 * @param {string} [options.element] the pill's element name
 * @returns {HTMLElement}
 */
export function tagPill(tag, { pillClass, buttonClass, dataKey, element = 'div' }) {
    const pill = document.createElement(element);
    pill.className = pillClass;

    const label = document.createElement('span');
    label.textContent = tag;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = buttonClass;
    button.dataset[dataKey] = tag;
    button.innerHTML = REMOVE_ICON;

    pill.append(label, button);
    return pill;
}
