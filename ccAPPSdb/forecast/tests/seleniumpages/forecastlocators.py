from selenium.webdriver.common.by import By


class ForecastTableLocators(object):
    TABLE_DEFAULT = (By.CSS_SELECTOR, "table[id='fforecasttable']")

    TABLE_SAVE_BUTTON = (By.CSS_SELECTOR, 'button[id="save"]')
    TABLE_UNDO_BUTTON = (By.CSS_SELECTOR, 'button[id="undo"]')
    FORECAST_OVERRIDE_EDIT_APPLY_BUTTON = (By.CSS_SELECTOR, 'button[id="applyedit"]')

    FORECAST_OVERRIDE_CELLS = (By.CSS_SELECTOR, 'input[tabindex="7"]')
    FORECAST_OVERRIDE_STARTDATE = (
        By.CSS_SELECTOR,
        'input[id="editstartdate"]',
    )
    FORECAST_OVERRIDE_ENDDATE = (By.CSS_SELECTOR, 'input[id="editenddate"]')
    FORECAST_OVERRIDE_SET_INPUT = (By.CSS_SELECTOR, 'input[id="editset"]')
