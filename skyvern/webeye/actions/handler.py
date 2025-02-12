import asyncio
import copy
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List

import structlog
from deprecation import deprecated
from playwright.async_api import FileChooser, Locator, Page, TimeoutError
from pydantic import BaseModel

from skyvern.constants import REPO_ROOT_DIR, SKYVERN_ID_ATTR, VERIFICATION_CODE_POLLING_TIMEOUT_MINS
from skyvern.exceptions import (
    EmptySelect,
    ErrEmptyTweakValue,
    ErrFoundSelectableElement,
    FailedToFetchSecret,
    FailToClick,
    FailToFindAutocompleteOption,
    FailToSelectByIndex,
    FailToSelectByLabel,
    FailToSelectByValue,
    ImaginaryFileUrl,
    InvalidElementForTextInput,
    MissingElement,
    MissingFileUrl,
    MultipleElementsFound,
    NoAutoCompleteOptionMeetCondition,
    NoElementMatchedForTargetOption,
    NoIncrementalElementFoundForAutoCompletion,
    NoIncrementalElementFoundForCustomSelection,
    NoLabelOrValueForCustomSelection,
    NoSuitableAutoCompleteOption,
    OptionIndexOutOfBound,
    WrongElementToUploadFile,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.files import (
    download_file,
    get_number_of_files_in_directory,
    get_path_for_workflow_download_directory,
)
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandler
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_post
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CheckboxAction,
    ClickAction,
    ScrapeResult,
    SelectOption,
    SelectOptionAction,
    UploadFileAction,
    WebAction,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.browser_factory import BrowserState, get_download_dir
from skyvern.webeye.scraper.scraper import (
    ElementTreeFormat,
    IncrementalScrapePage,
    ScrapedPage,
    json_to_html,
    trim_element_tree,
)
from skyvern.webeye.utils.dom import DomUtil, InteractiveElement, SkyvernElement
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()
COMMON_INPUT_TAGS = {"input", "textarea", "select"}


class AutoCompletionResult(BaseModel):
    auto_completion_attempt: bool = False
    incremental_elements: list[dict] = []
    action_result: ActionResult = ActionSuccess()


class ActionHandler:
    _handled_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _setup_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    _teardown_action_types: dict[
        ActionType,
        Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ] = {}

    @classmethod
    def register_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._handled_action_types[action_type] = handler

    @classmethod
    def register_setup_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._setup_action_types[action_type] = handler

    @classmethod
    def register_teardown_for_action_type(
        cls,
        action_type: ActionType,
        handler: Callable[[Action, Page, ScrapedPage, Task, Step], Awaitable[list[ActionResult]]],
    ) -> None:
        cls._teardown_action_types[action_type] = handler

    @staticmethod
    async def handle_action(
        scraped_page: ScrapedPage,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        action: Action,
    ) -> list[ActionResult]:
        LOG.info("Handling action", action=action)
        page = await browser_state.get_or_create_page()
        try:
            if action.action_type in ActionHandler._handled_action_types:
                actions_result: list[ActionResult] = []

                if invalid_web_action_check := check_for_invalid_web_action(action, page, scraped_page, task, step):
                    return invalid_web_action_check

                # do setup before action handler
                if setup := ActionHandler._setup_action_types.get(action.action_type):
                    results = await setup(action, page, scraped_page, task, step)
                    actions_result.extend(results)
                    if results and results[-1] != ActionSuccess:
                        return actions_result

                # do the handler
                handler = ActionHandler._handled_action_types[action.action_type]
                results = await handler(action, page, scraped_page, task, step)
                actions_result.extend(results)
                if not results or type(actions_result[-1]) != ActionSuccess:
                    return actions_result

                # do the teardown
                teardown = ActionHandler._teardown_action_types.get(action.action_type)
                if not teardown:
                    return actions_result

                results = await teardown(action, page, scraped_page, task, step)
                actions_result.extend(results)
                return actions_result

            else:
                LOG.error(
                    "Unsupported action type in handler",
                    action=action,
                    type=type(action),
                )
                return [ActionFailure(Exception(f"Unsupported action type: {type(action)}"))]
        except MissingElement as e:
            LOG.info(
                "Known exceptions",
                action=action,
                exception_type=type(e),
                exception_message=str(e),
            )
            return [ActionFailure(e)]
        except MultipleElementsFound as e:
            LOG.exception(
                "Cannot handle multiple elements with the same selector in one action.",
                action=action,
            )
            return [ActionFailure(e)]
        except Exception as e:
            LOG.exception("Unhandled exception in action handler", action=action)
            return [ActionFailure(e)]


def check_for_invalid_web_action(
    action: actions.Action,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if isinstance(action, WebAction) and action.element_id not in scraped_page.id_to_element_dict:
        return [ActionFailure(MissingElement(element_id=action.element_id), stop_execution_on_failure=False)]

    return []


async def handle_solve_captcha_action(
    action: actions.SolveCaptchaAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    LOG.warning(
        "Please solve the captcha on the page, you have 30 seconds",
        action=action,
    )
    await asyncio.sleep(30)
    return [ActionSuccess()]


async def handle_click_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    num_downloaded_files_before = 0
    download_dir = None
    if task.workflow_run_id:
        download_dir = get_path_for_workflow_download_directory(task.workflow_run_id)
        num_downloaded_files_before = get_number_of_files_in_directory(download_dir)
        LOG.info(
            "Number of files in download directory before click",
            num_downloaded_files_before=num_downloaded_files_before,
            download_dir=download_dir,
        )
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    await asyncio.sleep(0.3)
    if action.download:
        results = await handle_click_to_download_file_action(action, page, scraped_page, task)
    else:
        results = await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )

    if results and task.workflow_run_id and download_dir:
        LOG.info("Sleeping for 5 seconds to let the download finish")
        await asyncio.sleep(5)
        num_downloaded_files_after = get_number_of_files_in_directory(download_dir)
        LOG.info(
            "Number of files in download directory after click",
            num_downloaded_files_after=num_downloaded_files_after,
            download_dir=download_dir,
        )
        if num_downloaded_files_after > num_downloaded_files_before:
            results[-1].download_triggered = True

    return results


async def handle_click_to_download_file_action(
    action: actions.ClickAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    try:
        await locator.click(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)

        await page.wait_for_load_state(timeout=SettingsManager.get_settings().BROWSER_LOADING_TIMEOUT_MS)
        # TODO: shall we back to the previous page ?
        if await SkyvernFrame.get_print_triggered(page):
            path = f"{get_download_dir(task.workflow_run_id, task.task_id)}/{uuid.uuid4()}"
            LOG.warning(
                "Trying to download the printed PDF",
                path=path,
                action=action,
            )
            await page.pdf(format="A4", display_header_footer=True, path=path)
            await SkyvernFrame.reset_print_triggered(page)

    except Exception as e:
        LOG.exception("ClickAction with download failed", action=action, exc_info=True)
        return [ActionFailure(e, download_triggered=False)]

    return [ActionSuccess()]


async def handle_input_text_action(
    action: actions.InputTextAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS

    current_text = await get_input_value(skyvern_element.get_tag_name(), skyvern_element.get_locator())
    if current_text == action.text:
        return [ActionSuccess()]

    # before filling text, we need to validate if the element can be filled if it's not one of COMMON_INPUT_TAGS
    tag_name = scraped_page.id_to_element_dict[action.element_id]["tagName"].lower()
    text = await get_actual_value_of_parameter_if_secret(task, action.text)
    if text is None:
        return [ActionFailure(FailedToFetchSecret())]

    # check if it's selectable
    if skyvern_element.get_tag_name() == InteractiveElement.INPUT:
        await skyvern_element.scroll_into_view()
        select_action = SelectOptionAction(
            reasoning=action.reasoning, element_id=skyvern_element.get_id(), option=SelectOption(label=text)
        )
        if skyvern_element.get_selectable():
            LOG.info(
                "Input element is selectable, doing select actions",
                task_id=task.task_id,
                step_id=step.step_id,
                element_id=skyvern_element.get_id(),
                action=action,
            )
            return await handle_select_option_action(select_action, page, scraped_page, task, step)

        # press arrowdown to watch if there's any options popping up
        await incremental_scraped.start_listen_dom_increment()
        await skyvern_element.get_locator().focus(timeout=timeout)
        await skyvern_element.get_locator().press("ArrowDown", timeout=timeout)
        await asyncio.sleep(5)

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)
        )
        if len(incremental_element) == 0:
            LOG.info(
                "No new element detected, indicating it couldn't be a selectable auto-completion input",
                task_id=task.task_id,
                step_id=step.step_id,
                element_id=skyvern_element.get_id(),
                action=action,
            )
        else:
            try:
                result = await select_from_dropdown(
                    action=select_action,
                    page=page,
                    dom=dom,
                    skyvern_frame=skyvern_frame,
                    incremental_scraped=incremental_scraped,
                    element_trees=incremental_element,
                    llm_handler=app.SECONDARY_LLM_API_HANDLER,
                    step=step,
                    task=task,
                )
                if result is not None:
                    return [result]
                LOG.info(
                    "No dropdown menu detected, indicating it couldn't be a selectable auto-completion input",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    element_id=skyvern_element.get_id(),
                    action=action,
                )
            except Exception as e:
                await skyvern_element.scroll_into_view()
                LOG.exception("Failed to do custom selection transformed from input action")
                return [ActionFailure(exception=e)]
            finally:
                await skyvern_element.press_key("Escape")
                await skyvern_element.blur()
                await incremental_scraped.stop_listen_dom_increment()

    # force to move focus back to the element
    await skyvern_element.get_locator().focus(timeout=timeout)
    try:
        await skyvern_element.input_clear()
    except TimeoutError:
        LOG.info("None input tag clear timeout", action=action)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]
    except Exception:
        LOG.warning("Failed to clear the input field", action=action, exc_info=True)
        return [ActionFailure(InvalidElementForTextInput(element_id=action.element_id, tag_name=tag_name))]

    # TODO: not sure if this case will trigger auto-completion
    if tag_name not in COMMON_INPUT_TAGS:
        await skyvern_element.input_fill(text)
        return [ActionSuccess()]

    if len(text) == 0:
        return [ActionSuccess()]

    if await skyvern_element.is_auto_completion_input():
        result = await input_or_auto_complete_input(
            action=action,
            page=page,
            dom=dom,
            text=text,
            skyvern_element=skyvern_element,
            step=step,
            task=task,
        )
        return [result]

    await skyvern_element.input_sequentially(text=text)
    return [ActionSuccess()]


async def handle_upload_file_action(
    action: actions.UploadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    if not action.file_url:
        LOG.warning("InputFileAction has no file_url", action=action)
        return [ActionFailure(MissingFileUrl())]
    # ************************************************************************************************************** #
    # After this point if the file_url is a secret, it will be replaced with the actual value
    # In order to make sure we don't log the secret value, we log the action with the original value action.file_url
    # ************************************************************************************************************** #
    file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
    decoded_url = urllib.parse.unquote(file_url)
    if file_url not in str(task.navigation_payload) and decoded_url not in str(task.navigation_payload):
        LOG.warning(
            "LLM might be imagining the file url, which is not in navigation payload",
            action=action,
            file_url=action.file_url,
        )
        return [ActionFailure(ImaginaryFileUrl(action.file_url))]

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    file_path = await download_file(file_url)
    is_file_input = await is_file_input_element(locator)

    if is_file_input:
        LOG.info("Taking UploadFileAction. Found file input tag", action=action)
        if file_path:
            await locator.set_input_files(
                file_path,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )

            # Sleep for 10 seconds after uploading a file to let the page process it
            await asyncio.sleep(10)

            return [ActionSuccess()]
        else:
            return [ActionFailure(Exception(f"Failed to download file from {action.file_url}"))]
    else:
        LOG.info("Taking UploadFileAction. Found non file input tag", action=action)
        # treat it as a click action
        action.is_upload_file_tag = False
        return await chain_click(
            task,
            scraped_page,
            page,
            action,
            skyvern_element,
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )


@deprecated("This function is deprecated. Downloads are handled by the click action handler now.")
async def handle_download_file_action(
    action: actions.DownloadFileAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    file_name = f"{action.file_name or uuid.uuid4()}"
    full_file_path = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}/{file_name}"
    try:
        # Start waiting for the download
        async with page.expect_download() as download_info:
            await asyncio.sleep(0.3)

            locator = skyvern_element.locator
            await locator.click(
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
                modifiers=["Alt"],
            )

        download = await download_info.value

        # Create download folders if they don't exist
        download_folder = f"{REPO_ROOT_DIR}/downloads/{task.workflow_run_id or task.task_id}"
        os.makedirs(download_folder, exist_ok=True)
        # Wait for the download process to complete and save the downloaded file
        await download.save_as(full_file_path)
    except Exception as e:
        LOG.exception(
            "DownloadFileAction: Failed to download file",
            action=action,
            full_file_path=full_file_path,
        )
        return [ActionFailure(e)]

    return [ActionSuccess(data={"file_path": full_file_path})]


async def handle_null_action(
    action: actions.NullAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_select_option_action(
    action: actions.SelectOptionAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    dom = DomUtil(scraped_page, page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)

    tag_name = skyvern_element.get_tag_name()
    element_dict = scraped_page.id_to_element_dict[action.element_id]
    LOG.info(
        "SelectOptionAction",
        action=action,
        tag_name=tag_name,
        element_dict=element_dict,
    )

    if not await skyvern_element.is_selectable():
        # 1. find from children
        # TODO: 2. find from siblings and their chidren
        LOG.info(
            "Element is not selectable, try to find the selectable element in the chidren",
            tag_name=tag_name,
            action=action,
        )

        selectable_child: SkyvernElement | None = None
        try:
            selectable_child = await skyvern_element.find_selectable_child(dom=dom)
        except Exception as e:
            LOG.error(
                "Failed to find selectable element in chidren",
                exc_info=True,
                tag_name=tag_name,
                action=action,
            )
            return [ActionFailure(ErrFoundSelectableElement(action.element_id, e))]

        if selectable_child:
            LOG.info(
                "Found selectable element in the children",
                tag_name=selectable_child.get_tag_name(),
                element_id=selectable_child.get_id(),
            )
            select_action = SelectOptionAction(element_id=selectable_child.get_id(), option=action.option)
            return await handle_select_option_action(select_action, page, scraped_page, task, step)

    if tag_name == InteractiveElement.SELECT:
        LOG.info(
            "SelectOptionAction is on <select>",
            action=action,
        )
        return await normal_select(action=action, skyvern_element=skyvern_element)

    if await skyvern_element.is_checkbox():
        LOG.info(
            "SelectOptionAction is on <input> checkbox",
            action=action,
        )
        check_action = CheckboxAction(element_id=action.element_id, is_checked=True)
        return await handle_checkbox_action(check_action, page, scraped_page, task, step)

    if await skyvern_element.is_radio():
        LOG.info(
            "SelectOptionAction is on <input> radio",
            action=action,
        )
        click_action = ClickAction(element_id=action.element_id)
        return await chain_click(task, scraped_page, page, click_action, skyvern_element)

    LOG.info(
        "Trigger custom select",
        action=action,
    )

    timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS
    skyvern_frame = await SkyvernFrame.create_instance(skyvern_element.get_frame())
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    is_open = False

    try:
        await incremental_scraped.start_listen_dom_increment()
        await skyvern_element.focus()

        try:
            await skyvern_element.get_locator().click(timeout=timeout)
        except Exception:
            LOG.info(
                "fail to open dropdown by clicking, try to press ArrowDown to open",
                element_id=skyvern_element.get_id(),
                task_id=task.task_id,
                step_id=step.step_id,
            )
            await skyvern_element.focus()
            await skyvern_element.press_key("ArrowDown")

        # wait 5s for options to load
        await asyncio.sleep(5)
        is_open = True

        incremental_element = await incremental_scraped.get_incremental_element_tree(
            app.AGENT_FUNCTION.cleanup_element_tree_factory(step=step, task=task)
        )
        if len(incremental_element) == 0:
            raise NoIncrementalElementFoundForCustomSelection(element_id=action.element_id)

        result = await select_from_dropdown(
            action=action,
            page=page,
            dom=dom,
            skyvern_frame=skyvern_frame,
            incremental_scraped=incremental_scraped,
            element_trees=incremental_element,
            llm_handler=app.SECONDARY_LLM_API_HANDLER,
            step=step,
            task=task,
            force_select=True,
        )
        # force_select won't return None result
        assert result is not None
        return [result]

    except Exception as e:
        if is_open:
            await skyvern_element.scroll_into_view()
            await skyvern_element.coordinate_click(page=page)
            await skyvern_element.get_locator().press("Escape", timeout=timeout)
        LOG.exception("Custom select error")
        return [ActionFailure(exception=e)]
    finally:
        await incremental_scraped.stop_listen_dom_increment()


async def handle_checkbox_action(
    action: actions.CheckboxAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    """
    ******* NOT REGISTERED *******
    This action causes more harm than it does good.
    It frequently mis-behaves, or gets stuck in click loops.
    Treating checkbox actions as click actions seem to perform way more reliably
    Developers who tried this and failed: 2 (Suchintan and Shu 😂)
    """

    dom = DomUtil(scraped_page=scraped_page, page=page)
    skyvern_element = await dom.get_skyvern_element_by_id(action.element_id)
    locator = skyvern_element.locator

    if action.is_checked:
        await locator.check(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
    else:
        await locator.uncheck(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)

    # TODO (suchintan): Why does checking the label work, but not the actual input element?
    return [ActionSuccess()]


async def handle_wait_action(
    action: actions.WaitAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    await asyncio.sleep(10)
    return [ActionFailure(exception=Exception("Wait action is treated as a failure"))]


async def handle_terminate_action(
    action: actions.TerminateAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    return [ActionSuccess()]


async def handle_complete_action(
    action: actions.CompleteAction,
    page: Page,
    scraped_page: ScrapedPage,
    task: Task,
    step: Step,
) -> list[ActionResult]:
    extracted_data = None
    if action.data_extraction_goal:
        scrape_action_result = await extract_information_for_navigation_goal(
            scraped_page=scraped_page,
            task=task,
            step=step,
        )
        extracted_data = scrape_action_result.scraped_data
    return [ActionSuccess(data=extracted_data)]


ActionHandler.register_action_type(ActionType.SOLVE_CAPTCHA, handle_solve_captcha_action)
ActionHandler.register_action_type(ActionType.CLICK, handle_click_action)
ActionHandler.register_action_type(ActionType.INPUT_TEXT, handle_input_text_action)
ActionHandler.register_action_type(ActionType.UPLOAD_FILE, handle_upload_file_action)
# ActionHandler.register_action_type(ActionType.DOWNLOAD_FILE, handle_download_file_action)
ActionHandler.register_action_type(ActionType.NULL_ACTION, handle_null_action)
ActionHandler.register_action_type(ActionType.SELECT_OPTION, handle_select_option_action)
ActionHandler.register_action_type(ActionType.WAIT, handle_wait_action)
ActionHandler.register_action_type(ActionType.TERMINATE, handle_terminate_action)
ActionHandler.register_action_type(ActionType.COMPLETE, handle_complete_action)


async def get_actual_value_of_parameter_if_secret(task: Task, parameter: str) -> Any:
    """
    Get the actual value of a parameter if it's a secret. If it's not a secret, return the parameter value as is.

    Just return the parameter value if the task isn't a workflow's task.

    This is only used for InputTextAction, UploadFileAction, and ClickAction (if it has a file_url).
    """
    if task.workflow_run_id is None:
        return parameter

    workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(task.workflow_run_id)
    secret_value = workflow_run_context.get_original_secret_value_or_none(parameter)

    if secret_value == BitwardenConstants.TOTP:
        secrets = await workflow_run_context.get_secrets_from_password_manager()
        secret_value = secrets[BitwardenConstants.TOTP]
    return secret_value if secret_value is not None else parameter


async def chain_click(
    task: Task,
    scraped_page: ScrapedPage,
    page: Page,
    action: ClickAction | UploadFileAction,
    skyvern_element: SkyvernElement,
    timeout: int = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
) -> List[ActionResult]:
    # Add a defensive page handler here in case a click action opens a file chooser.
    # This automatically dismisses the dialog
    # File choosers are impossible to close if you don't expect one. Instead of dealing with it, close it!

    locator = skyvern_element.locator
    # TODO (suchintan): This should likely result in an ActionFailure -- we can figure out how to do this later!
    LOG.info("Chain click starts", action=action, locator=locator)
    file: list[str] | str = []
    if action.file_url:
        file_url = await get_actual_value_of_parameter_if_secret(task, action.file_url)
        try:
            file = await download_file(file_url)
        except Exception:
            LOG.exception(
                "Failed to download file, continuing without it",
                action=action,
                file_url=file_url,
            )
            file = []

    is_filechooser_trigger = False

    async def fc_func(fc: FileChooser) -> None:
        nonlocal is_filechooser_trigger
        is_filechooser_trigger = True
        await fc.set_files(files=file)

    page.on("filechooser", fc_func)
    LOG.info("Registered file chooser listener", action=action, path=file)

    """
    Clicks on an element identified by the css and its parent if failed.
    :param css: css of the element to click
    """
    javascript_triggered = await is_javascript_triggered(scraped_page, page, locator)
    try:
        await locator.click(timeout=timeout)

        LOG.info("Chain click: main element click succeeded", action=action, locator=locator)
        return [
            ActionSuccess(
                javascript_triggered=javascript_triggered,
            )
        ]

    except Exception:
        action_results: list[ActionResult] = [
            ActionFailure(
                FailToClick(action.element_id),
                javascript_triggered=javascript_triggered,
            )
        ]
        if await is_input_element(locator):
            LOG.info(
                "Chain click: it's an input element. going to try sibling click",
                action=action,
                locator=locator,
            )
            sibling_action_result = await click_sibling_of_input(locator, timeout=timeout)
            action_results.append(sibling_action_result)
            if type(sibling_action_result) == ActionSuccess:
                return action_results

        try:
            parent_locator = locator.locator("..")

            parent_javascript_triggered = await is_javascript_triggered(scraped_page, page, parent_locator)
            javascript_triggered = javascript_triggered or parent_javascript_triggered

            await parent_locator.click(timeout=timeout)

            LOG.info(
                "Chain click: successfully clicked parent element",
                action=action,
                parent_locator=parent_locator,
            )
            action_results.append(
                ActionSuccess(
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
        except Exception:
            LOG.warning(
                "Failed to click parent element",
                action=action,
                parent_locator=parent_locator,
                exc_info=True,
            )
            action_results.append(
                ActionFailure(
                    FailToClick(action.element_id),
                    javascript_triggered=javascript_triggered,
                    interacted_with_parent=True,
                )
            )
            # We don't raise exception here because we do log the exception, and return ActionFailure as the last action

        return action_results
    finally:
        LOG.info("Remove file chooser listener", action=action)

        # Sleep for 15 seconds after uploading a file to let the page process it
        # Removing this breaks file uploads using the filechooser
        # KEREM DO NOT REMOVE
        if file:
            await asyncio.sleep(15)
        page.remove_listener("filechooser", fc_func)

        if action.file_url and not is_filechooser_trigger:
            LOG.warning(
                "Action has file_url, but filechoose even hasn't been triggered. Upload file attempt seems to fail",
                action=action,
            )
            return [ActionFailure(WrongElementToUploadFile(action.element_id))]


def remove_exist_elements(dom: DomUtil, element_tree: list[dict]) -> list[dict]:
    new_element_tree = []
    for element in element_tree:
        children_elements = element.get("children", [])
        if len(children_elements) > 0:
            children_elements = remove_exist_elements(dom=dom, element_tree=children_elements)
        if dom.check_id_in_dom(element.get("id", "")):
            new_element_tree.extend(children_elements)
        else:
            element["children"] = children_elements
            new_element_tree.append(element)
    return new_element_tree


async def choose_auto_completion_dropdown(
    action: actions.InputTextAction,
    page: Page,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
    preserved_elements: list[dict] | None = None,
    relevance_threshold: float = 0.8,
) -> AutoCompletionResult:
    preserved_elements = preserved_elements or []
    clear_input = True
    result = AutoCompletionResult()

    current_frame = skyvern_element.get_frame()
    skyvern_frame = await SkyvernFrame.create_instance(current_frame)
    incremental_scraped = IncrementalScrapePage(skyvern_frame=skyvern_frame)
    await incremental_scraped.start_listen_dom_increment()

    try:
        await skyvern_element.press_fill(text)
        # wait for new elemnts to load
        await asyncio.sleep(5)
        incremental_element = await incremental_scraped.get_incremental_element_tree(
            app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)
        )
        incremental_element = remove_exist_elements(dom=dom, element_tree=incremental_element)

        # check if elements in preserve list are still on the page
        confirmed_preserved_list: list[dict] = []
        for element in preserved_elements:
            element_id = element.get("id")
            if not element_id:
                continue
            locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
            cnt = await locator.count()
            if cnt == 0:
                continue

            element_handler = await locator.element_handle(
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS
            )
            if not element_handler:
                continue

            current_element = await skyvern_frame.parse_element_from_html(
                skyvern_element.get_frame_id(), element_handler, skyvern_element.is_interactable()
            )
            confirmed_preserved_list.append(current_element)

        if len(confirmed_preserved_list) > 0:
            confirmed_preserved_list = await app.AGENT_FUNCTION.cleanup_element_tree_factory(task=task, step=step)(
                skyvern_frame.get_frame().url, copy.deepcopy(confirmed_preserved_list)
            )
            confirmed_preserved_list = trim_element_tree(copy.deepcopy(confirmed_preserved_list))

        incremental_element.extend(confirmed_preserved_list)

        result.incremental_elements = copy.deepcopy(incremental_element)
        if len(incremental_element) == 0:
            raise NoIncrementalElementFoundForAutoCompletion(element_id=skyvern_element.get_id(), text=text)

        html = incremental_scraped.build_html_tree(incremental_element)
        auto_completion_confirm_prompt = prompt_engine.load_prompt(
            "auto-completion-choose-option",
            context_reasoning=action.reasoning,
            filled_value=text,
            elements=html,
        )
        LOG.info(
            "Confirm if it's an auto completion dropdown",
            step_id=step.step_id,
            task_id=task.task_id,
        )
        json_response = await app.SECONDARY_LLM_API_HANDLER(prompt=auto_completion_confirm_prompt, step=step)
        element_id = json_response.get("id", "")
        relevance_float = json_response.get("relevance_float", 0)
        if not element_id:
            reasoning = json_response.get("reasoning")
            raise NoSuitableAutoCompleteOption(reasoning=reasoning, target_value=text)

        if relevance_float < relevance_threshold:
            LOG.info(
                f"The closest option doesn't meet the condition(relevance_float>={relevance_threshold})",
                element_id=element_id,
                relevance_float=relevance_float,
            )
            reasoning = json_response.get("reasoning")
            raise NoAutoCompleteOptionMeetCondition(
                reasoning=reasoning,
                required_relevance=relevance_threshold,
                target_value=text,
                closest_relevance=relevance_float,
            )

        LOG.info(
            "Find a suitable option to choose",
            element_id=element_id,
            step_id=step.step_id,
            task_id=task.task_id,
        )

        locator = current_frame.locator(f'[{SKYVERN_ID_ATTR}="{element_id}"]')
        if await locator.count() == 0:
            raise MissingElement(element_id=element_id)

        await locator.click(timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS)
        clear_input = False
        return result
    except Exception as e:
        LOG.info(
            "Failed to choose the auto completion dropdown",
            exc_info=True,
            input_value=text,
            task_id=task.task_id,
            step_id=step.step_id,
        )
        result.action_result = ActionFailure(exception=e)
        return result
    finally:
        await incremental_scraped.stop_listen_dom_increment()
        if clear_input:
            await skyvern_element.input_clear()


async def input_or_auto_complete_input(
    action: actions.InputTextAction,
    page: Page,
    dom: DomUtil,
    text: str,
    skyvern_element: SkyvernElement,
    step: Step,
    task: Task,
) -> ActionResult:
    LOG.info(
        "Trigger auto completion",
        task_id=task.task_id,
        step_id=step.step_id,
        element_id=skyvern_element.get_id(),
    )

    # 1. press the orignal text to see if there's a match
    # 2. call LLM to find 5 potential values based on the orginal text
    # 3. try each potential values from #2
    # 4. call LLM to tweak the orignal text according to the information from #3, then start #1 again

    # FIXME: try the whole loop for twice now, to prevent too many LLM calls
    MAX_AUTO_COMPLETE_ATTEMP = 2
    current_attemp = 0
    context_reasoning = action.reasoning
    current_value = text
    result = AutoCompletionResult()

    while current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
        current_attemp += 1
        whole_new_elements: list[dict] = []
        tried_values: list[str] = []

        LOG.info(
            "Try the potential value for auto completion",
            step_id=step.step_id,
            task_id=task.task_id,
            input_value=current_value,
        )
        result = await choose_auto_completion_dropdown(
            action=action,
            page=page,
            dom=dom,
            text=current_value,
            preserved_elements=result.incremental_elements,
            skyvern_element=skyvern_element,
            step=step,
            task=task,
        )
        if isinstance(result.action_result, ActionSuccess):
            return ActionSuccess()

        tried_values.append(current_value)
        whole_new_elements.extend(result.incremental_elements)

        prompt = prompt_engine.load_prompt(
            "auto-completion-potential-answers",
            context_reasoning=context_reasoning,
            current_value=current_value,
        )

        LOG.info(
            "Ask LLM to give 10 potential values based on the current value",
            current_value=current_value,
            step_id=step.step_id,
            task_id=task.task_id,
        )
        json_respone = await app.SECONDARY_LLM_API_HANDLER(prompt=prompt, step=step)
        values: list[dict] = json_respone.get("potential_values", [])

        for each_value in values:
            value: str = each_value.get("value", "")
            if not value:
                LOG.info(
                    "Empty potential value, skip this attempt",
                    step_id=step.step_id,
                    task_id=task.task_id,
                    value=each_value,
                )
                continue
            LOG.info(
                "Try the potential value for auto completion",
                step_id=step.step_id,
                task_id=task.task_id,
                input_value=value,
            )
            result = await choose_auto_completion_dropdown(
                action=action,
                page=page,
                dom=dom,
                text=value,
                preserved_elements=result.incremental_elements,
                skyvern_element=skyvern_element,
                step=step,
                task=task,
            )
            if isinstance(result.action_result, ActionSuccess):
                return ActionSuccess()

            tried_values.append(value)
            whole_new_elements.extend(result.incremental_elements)

        if current_attemp < MAX_AUTO_COMPLETE_ATTEMP:
            LOG.info(
                "Ask LLM to tweak the current value based on tried input values",
                step_id=step.step_id,
                task_id=task.task_id,
                current_value=current_value,
                current_attemp=current_attemp,
            )
            prompt = prompt_engine.load_prompt(
                "auto-completion-tweak-value",
                context_reasoning=context_reasoning,
                current_value=current_value,
                tried_values=json.dumps(tried_values),
                popped_up_elements="".join([json_to_html(element) for element in whole_new_elements]),
            )
            json_respone = await app.SECONDARY_LLM_API_HANDLER(prompt=prompt, step=step)
            context_reasoning = json_respone.get("reasoning")
            new_current_value = json_respone.get("tweaked_value", "")
            if not new_current_value:
                return ActionFailure(ErrEmptyTweakValue(reasoning=context_reasoning, current_value=current_value))
            LOG.info(
                "Ask LLM tweaked the current value with a new value",
                step_id=step.step_id,
                task_id=task.task_id,
                reasoning=context_reasoning,
                current_value=current_value,
                new_value=new_current_value,
            )
            current_value = new_current_value

    else:
        return ActionFailure(FailToFindAutocompleteOption(current_value=text))


async def select_from_dropdown(
    action: SelectOptionAction,
    page: Page,
    dom: DomUtil,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    element_trees: list[dict],
    llm_handler: LLMAPIHandler,
    step: Step,
    task: Task,
    force_select: bool = False,
) -> ActionResult | None:
    """
    force_select is used to choose an element to click even there's no dropdown menu
    None will be only returned when force_select is false and no dropdown menu popped
    """
    timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS

    dropdown_menu_element = await locate_dropdown_meanu(
        incremental_scraped=incremental_scraped,
        element_trees=element_trees,
        llm_handler=llm_handler,
        step=step,
        task=task,
    )

    if not force_select and dropdown_menu_element is None:
        return None

    if dropdown_menu_element and await skyvern_frame.get_element_scrollable(
        await dropdown_menu_element.get_element_handler()
    ):
        await scroll_down_to_load_all_options(
            dropdown_menu_element=dropdown_menu_element,
            skyvern_frame=skyvern_frame,
            page=page,
            incremental_scraped=incremental_scraped,
            step=step,
            task=task,
        )

    trimmed_element_tree = await incremental_scraped.get_incremental_element_tree(
        app.AGENT_FUNCTION.cleanup_element_tree_factory(step=step, task=task)
    )
    trimmed_element_tree = remove_exist_elements(dom=dom, element_tree=trimmed_element_tree)

    html = incremental_scraped.build_html_tree(element_tree=trimmed_element_tree)

    target_value = action.option.label or action.option.value
    if target_value is None:
        raise NoLabelOrValueForCustomSelection(element_id=action.element_id)

    prompt = prompt_engine.load_prompt(
        "custom-select", context_reasoning=action.reasoning, target_value=target_value, elements=html
    )

    LOG.info(
        "Calling LLM to find the match element",
        target_value=target_value,
        step_id=step.step_id,
        task_id=task.task_id,
    )
    json_response = await llm_handler(prompt=prompt, step=step)
    LOG.info(
        "LLM response for the matched element",
        target_value=target_value,
        response=json_response,
        step_id=step.step_id,
        task_id=task.task_id,
    )

    element_id: str | None = json_response.get("id", None)
    if not element_id:
        raise NoElementMatchedForTargetOption(target=target_value, reason=json_response.get("reasoning"))

    selected_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
    await selected_element.scroll_into_view()
    await selected_element.get_locator().click(timeout=timeout)
    return ActionSuccess()


async def locate_dropdown_meanu(
    incremental_scraped: IncrementalScrapePage,
    element_trees: list[dict],
    llm_handler: LLMAPIHandler,
    step: Step | None = None,
    task: Task | None = None,
) -> SkyvernElement | None:
    for idx, element_dict in enumerate(element_trees):
        # FIXME: confirm max to 10 nodes for now, preventing sendindg too many requests to LLM
        if idx >= 10:
            break

        element_id = element_dict.get("id")
        if not element_id:
            LOG.info(
                "Skip the non-interactable element for the dropdown menu confirm",
                step_id=step.step_id if step else "none",
                task_id=task.task_id if task else "none",
                element=element_dict,
            )
            continue
        head_element = await SkyvernElement.create_from_incremental(incremental_scraped, element_id)
        screenshot = await head_element.get_locator().screenshot(
            timeout=SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS
        )
        dropdown_confirm_prompt = prompt_engine.load_prompt("opened-dropdown-confirm")
        LOG.info(
            "Confirm if it's an opened dropdown menu",
            step_id=step.step_id if step else "none",
            task_id=task.task_id if task else "none",
            element=element_dict,
        )
        json_response = await llm_handler(prompt=dropdown_confirm_prompt, screenshots=[screenshot], step=step)
        is_opened_dropdown_menu = json_response.get("is_opened_dropdown_menu")
        if is_opened_dropdown_menu:
            return await SkyvernElement.create_from_incremental(incre_page=incremental_scraped, element_id=element_id)
    return None


async def scroll_down_to_load_all_options(
    dropdown_menu_element: SkyvernElement,
    page: Page,
    skyvern_frame: SkyvernFrame,
    incremental_scraped: IncrementalScrapePage,
    step: Step | None = None,
    task: Task | None = None,
) -> None:
    LOG.info(
        "Scroll down the dropdown menu to load all options",
        step_id=step.step_id if step else "none",
        task_id=task.task_id if task else "none",
    )
    timeout = SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS

    dropdown_menu_element_handle = await dropdown_menu_element.get_locator().element_handle(timeout=timeout)
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using focus to move the cursor", element_id=dropdown_menu_element.get_id())
        await dropdown_menu_element.get_locator().focus(timeout=timeout)
    else:
        await dropdown_menu_element_handle.scroll_into_view_if_needed(timeout=timeout)

    await dropdown_menu_element.move_mouse_to(page=page)

    scroll_pace = 0
    previous_num = await incremental_scraped.get_incremental_elements_num()

    deadline = datetime.now(timezone.utc) + timedelta(
        milliseconds=SettingsManager.get_settings().OPTION_LOADING_TIMEOUT_MS
    )
    while datetime.now(timezone.utc) < deadline:
        # make sure we can scroll to the bottom
        scroll_interval = SettingsManager.get_settings().BROWSER_HEIGHT * 5
        if dropdown_menu_element_handle is None:
            LOG.info("element handle is None, using mouse to scroll down", element_id=dropdown_menu_element.get_id())
            await page.mouse.wheel(0, scroll_interval)
            scroll_pace += scroll_interval
        else:
            await skyvern_frame.scroll_to_element_bottom(dropdown_menu_element_handle)
            # wait for the options to be fully loaded
            await asyncio.sleep(2)

        # scoll a little back and scoll down to trigger the loading
        await page.mouse.wheel(0, -1e-5)
        await page.mouse.wheel(0, 1e-5)
        # wait for while to load new options
        await asyncio.sleep(10)

        current_num = await incremental_scraped.get_incremental_elements_num()
        LOG.info(
            "Current incremental elements count during the scrolling",
            num=current_num,
            step_id=step.step_id if step else "none",
            task_id=task.task_id if task else "none",
        )
        if previous_num == current_num:
            break
        previous_num = current_num
    else:
        LOG.warning("Timeout to load all options, maybe some options will be missed")

    # scoll back to the start point and wait for a while to make all options invisible on the page
    if dropdown_menu_element_handle is None:
        LOG.info("element handle is None, using mouse to scroll back", element_id=dropdown_menu_element.get_id())
        await page.mouse.wheel(0, -scroll_pace)
    else:
        await skyvern_frame.scroll_to_element_top(dropdown_menu_element_handle)
    await asyncio.sleep(5)


async def normal_select(
    action: actions.SelectOptionAction,
    skyvern_element: SkyvernElement,
) -> List[ActionResult]:
    try:
        current_text = await skyvern_element.get_attr("selected")
        if current_text == action.option.label or current_text == action.option.value:
            return [ActionSuccess()]
    except Exception:
        LOG.info("failed to confirm if the select option has been done, force to take the action again.")

    action_result: List[ActionResult] = []
    is_success = False
    locator = skyvern_element.get_locator()

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.error(
            "Failed to click before select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if not is_success and action.option.label is not None:
        try:
            # First click by label (if it matches)
            await locator.select_option(
                label=action.option.label,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByLabel(action.element_id)))
            LOG.error(
                "Failed to take select action by label",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and action.option.value is not None:
        try:
            # click by value (if it matches)
            await locator.select_option(
                value=action.option.value,
                timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            )
            is_success = True
            action_result.append(ActionSuccess())
        except Exception:
            action_result.append(ActionFailure(FailToSelectByValue(action.element_id)))
            LOG.error(
                "Failed to take select action by value",
                exc_info=True,
                action=action,
                locator=locator,
            )

    if not is_success and action.option.index is not None:
        if action.option.index >= len(skyvern_element.get_options()):
            action_result.append(ActionFailure(OptionIndexOutOfBound(action.element_id)))
            LOG.error(
                "option index is out of bound",
                action=action,
                locator=locator,
            )
        else:
            try:
                # This means the supplied index was for the select element, not a reference to the css dict
                await locator.select_option(
                    index=action.option.index,
                    timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
                )
                is_success = True
                action_result.append(ActionSuccess())
            except Exception:
                action_result.append(ActionFailure(FailToSelectByIndex(action.element_id)))
                LOG.error(
                    "Failed to click on the option by index",
                    exc_info=True,
                    action=action,
                    locator=locator,
                )

    try:
        await locator.click(
            timeout=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
        )
    except Exception as e:
        LOG.error(
            "Failed to click after select action",
            exc_info=True,
            action=action,
            locator=locator,
        )
        action_result.append(ActionFailure(e))
        return action_result

    if len(action_result) == 0:
        action_result.append(ActionFailure(EmptySelect(element_id=action.element_id)))

    return action_result


def get_anchor_to_click(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    Get the anchor tag under the label to click
    """
    LOG.info("Getting anchor tag to click", element_id=element_id)
    for ele in scraped_page.elements:
        if "id" in ele and ele["id"] == element_id:
            for child in ele["children"]:
                if "tagName" in child and child["tagName"] == "a":
                    return scraped_page.id_to_css_dict[child["id"]]
    return None


def get_select_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search <select> in the children of <label>
    """
    LOG.info("Searching select in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "select":
            return child.get("id", None)

    return None


def get_checkbox_id_in_label_children(scraped_page: ScrapedPage, element_id: str) -> str | None:
    """
    search checkbox/radio in the children of <label>
    """
    LOG.info("Searching checkbox/radio in the label children", element_id=element_id)
    element = scraped_page.id_to_element_dict.get(element_id, None)
    if element is None:
        return None

    for child in element.get("children", []):
        if child.get("tagName", "") == "input" and child.get("attributes", {}).get("type") in ["checkbox", "radio"]:
            return child.get("id", None)

    return None


@deprecated("This function is deprecated. It was used for select2 dropdown, but we don't use it anymore.")
async def is_javascript_triggered(scraped_page: ScrapedPage, page: Page, locator: Locator) -> bool:
    element = locator.first

    tag_name = await element.evaluate("e => e.tagName")
    if tag_name.lower() == "a":
        href = await element.evaluate("e => e.href")
        if href.lower().startswith("javascript:"):
            LOG.info("Found javascript call in anchor tag, marking step as completed. Dropping remaining actions")
            return True
    return False


async def get_tag_name_lowercase(locator: Locator) -> str | None:
    element = locator.first
    if element:
        tag_name = await element.evaluate("e => e.tagName")
        return tag_name.lower()
    return None


async def is_file_input_element(locator: Locator) -> bool:
    element = locator.first
    if element:
        tag_name = await element.evaluate("el => el.tagName")
        type_name = await element.evaluate("el => el.type")
        return tag_name.lower() == "input" and type_name == "file"
    return False


async def is_input_element(locator: Locator) -> bool:
    element = locator.first
    if element:
        tag_name = await element.evaluate("el => el.tagName")
        return tag_name.lower() == "input"
    return False


async def click_sibling_of_input(
    locator: Locator,
    timeout: int,
    javascript_triggered: bool = False,
) -> ActionResult:
    try:
        input_element = locator.first
        parent_locator = locator.locator("..")
        if input_element:
            input_id = await input_element.get_attribute("id")
            sibling_label_css = f'label[for="{input_id}"]'
            label_locator = parent_locator.locator(sibling_label_css)
            await label_locator.click(timeout=timeout)
            LOG.info(
                "Successfully clicked sibling label of input element",
                sibling_label_css=sibling_label_css,
            )
            return ActionSuccess(javascript_triggered=javascript_triggered, interacted_with_sibling=True)
        # Should never get here
        return ActionFailure(
            exception=Exception("Failed while trying to click sibling of input element"),
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=True,
        )
    except Exception:
        LOG.warning("Failed to click sibling label of input element", exc_info=True)
        return ActionFailure(
            exception=Exception("Failed while trying to click sibling of input element"),
            javascript_triggered=javascript_triggered,
        )


async def extract_information_for_navigation_goal(
    task: Task,
    step: Step,
    scraped_page: ScrapedPage,
) -> ScrapeResult:
    """
    Scrapes a webpage and returns the scraped response, including:
    1. JSON representation of what the user is seeing
    2. The scraped page
    """
    prompt_template = "extract-information"

    # TODO: we only use HTML element for now, introduce a way to switch in the future
    element_tree_format = ElementTreeFormat.HTML
    element_tree_in_prompt: str = scraped_page.build_element_tree(element_tree_format)

    extract_information_prompt = prompt_engine.load_prompt(
        prompt_template,
        navigation_goal=task.navigation_goal,
        navigation_payload=task.navigation_payload,
        elements=element_tree_in_prompt,
        data_extraction_goal=task.data_extraction_goal,
        extracted_information_schema=task.extracted_information_schema,
        current_url=scraped_page.url,
        extracted_text=scraped_page.extracted_text,
        error_code_mapping_str=(json.dumps(task.error_code_mapping) if task.error_code_mapping else None),
        utc_datetime=datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    )

    json_response = await app.LLM_API_HANDLER(
        prompt=extract_information_prompt,
        step=step,
        screenshots=scraped_page.screenshots,
    )

    return ScrapeResult(
        scraped_data=json_response,
    )


async def click_listbox_option(
    scraped_page: ScrapedPage,
    page: Page,
    action: actions.SelectOptionAction,
    listbox_element_id: str,
) -> bool:
    listbox_element = scraped_page.id_to_element_dict[listbox_element_id]
    # this is a listbox element, get all the children
    if "children" not in listbox_element:
        return False

    LOG.info("starting bfs", listbox_element_id=listbox_element_id)
    bfs_queue = [child for child in listbox_element["children"]]
    while bfs_queue:
        child = bfs_queue.pop(0)
        LOG.info("popped child", element_id=child["id"])
        if "attributes" in child and "role" in child["attributes"] and child["attributes"]["role"] == "option":
            LOG.info("found option", element_id=child["id"])
            text = child["text"] if "text" in child else ""
            if text and (text == action.option.label or text == action.option.value):
                dom = DomUtil(scraped_page=scraped_page, page=page)
                try:
                    skyvern_element = await dom.get_skyvern_element_by_id(child["id"])
                    locator = skyvern_element.locator
                    await locator.click(timeout=1000)

                    return True
                except Exception:
                    LOG.error(
                        "Failed to click on the option",
                        action=action,
                        exc_info=True,
                    )
        if "children" in child:
            bfs_queue.extend(child["children"])
    return False


async def get_input_value(tag_name: str, locator: Locator) -> str | None:
    if tag_name in COMMON_INPUT_TAGS:
        return await locator.input_value()
    # for span, div, p or other tags:
    return await locator.inner_text()


async def poll_verification_code(task_id: str, organization_id: str, url: str) -> str | None:
    timeout = timedelta(minutes=VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
    start_datetime = datetime.utcnow()
    timeout_datetime = start_datetime + timeout
    org_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api)
    if not org_token:
        LOG.error("Failed to get organization token when trying to get verification code")
        return None
    while True:
        # check timeout
        if datetime.utcnow() > timeout_datetime:
            return None
        request_data = {
            "task_id": task_id,
        }
        payload = json.dumps(request_data)
        signature = generate_skyvern_signature(
            payload=payload,
            api_key=org_token.token,
        )
        timestamp = str(int(datetime.utcnow().timestamp()))
        headers = {
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        }
        json_resp = await aiohttp_post(url=url, data=request_data, headers=headers, raise_exception=False)
        verification_code = json_resp.get("verification_code", None)
        if verification_code:
            LOG.info("Got verification code", verification_code=verification_code)
            return verification_code

        await asyncio.sleep(10)
