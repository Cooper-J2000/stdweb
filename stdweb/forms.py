from django import forms

from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Fieldset, Div, Row, Column, Submit, HTML
from crispy_forms.bootstrap import InlineField, PrependedText, InlineRadios

# from django_select2 import forms as s2forms

import json

from .processing import supported_filters, supported_catalogs, supported_catalogs_transients, supported_templates
from . import models


class MultipleChoiceFieldNoValidation(forms.MultipleChoiceField):
    def validate(self, value):
        pass


class CheckboxDropdown(forms.SelectMultiple):
    """Multi-select rendered as a compact Bootstrap dropdown of checkboxes."""

    def render(self, name, value, attrs=None, renderer=None):
        selected = {str(v) for v in (value or [])}
        options = [
            {'value': str(v), 'label': label, 'selected': str(v) in selected}
            for v, label in self.choices
        ]
        context = {
            'name': name,
            'options': options,
            'id': (attrs or {}).get('id') or ('id_' + name),
        }
        return mark_safe(render_to_string('widgets/checkbox_dropdown.html', context))


class UploadFileForm(forms.Form):
    file = forms.FileField(label="FITS 文件", required=False)
    local_file = forms.CharField(required=False, widget=forms.HiddenInput())
    local_filename = forms.CharField(required=False, label="FITS 文件", disabled=True) # Will not be sent
    local_files = MultipleChoiceFieldNoValidation(required=False, widget=forms.CheckboxSelectMultiple)
    preset = forms.ChoiceField(
        choices=[('','')],
        required=False, label="配置预设"
    )
    target = forms.CharField(
        required=False, empty_value=None, label="目标名称或坐标",
        widget=forms.Textarea(attrs={'rows':1, 'placeholder': '名称、坐标或 x=... y=... 像素位置，每行一个'})
    )

    do_inspect = forms.BooleanField(initial=False, required=False, label="检查")
    do_photometry = forms.BooleanField(initial=False, required=False, label="测光")
    do_simple_transients = forms.BooleanField(initial=False, required=False, label="简单暂现源检测")
    do_subtraction = forms.BooleanField(initial=False, required=False, label="模板相减")

    title = forms.CharField(max_length=150, required=False, label="可选标题或备注")

    ext = forms.ChoiceField(
        choices=[('auto', '自动（最后一个 HDU）')] + [(str(i), f'HDU {i} ({"PRIMARY" if i == 0 else "扩展"})') for i in range(0, 11)],
        initial='auto', required=False, label="FITS 扩展层 (HDU)",
    )

    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(), required=False, label="与用户组共享",
        widget=CheckboxDropdown,
    )

    # FIXME: changes to these fields should be reflected in views.upload_file() !!!
    stack_method = forms.ChoiceField(
        choices=[
            ('sum', '求和'),
            ('clipped_mean', 'Sigma 截尾均值'),
            ('median', '中位数'),
        ],
        required=False, label="叠加方法"
    )
    stack_subtract_bg = forms.BooleanField(initial=True, required=False, label="扣除背景")
    stack_mask_cosmics = forms.BooleanField(initial=False, required=False, label="宇宙线掩模")

    def __init__(self, *args, **kwargs):
        filename = kwargs.pop('filename', None)
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Scope sharing to groups the user may share with; drop the field
        # entirely when there are none, so nothing extra is shown.
        groups_qs = models.accessible_groups(user) if user is not None else Group.objects.none()
        if groups_qs.exists():
            self.fields['groups'].queryset = groups_qs
        else:
            del self.fields['groups']

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.form_action = 'upload'
        self.helper.field_template = 'crispy_field.html'

        if filename == '*':
            file_field = 'local_filename'
            self.fields['local_file'].initial = filename
            self.fields['local_filename'].initial = '请在上方选择一个或多个文件'
            submit = Submit('process_files', '处理所选文件', css_class='btn-primary')
        elif filename:
            file_field = 'local_filename'
            self.fields['local_file'].initial = filename
            self.fields['local_filename'].initial = filename
            submit = Submit('process', '处理此文件', css_class='btn-primary')
        else:
            file_field = 'file'
            self.fields['file'].required = True
            submit = Submit('upload', '上传', css_class='btn-primary')

        self.helper.layout = Layout(
            Row(
                Column(file_field, css_class="col-md"),
                'local_file',
                Column('preset', css_class="col-md-auto"),
                Column('groups', css_class="col-md-auto") if 'groups' in self.fields else None,
                Column(submit, css_class="col-md-auto mb-1"),
                Column(
                    Submit('stack_files', '叠加并处理', css_class='btn-secondary'),
                    css_class="col-md-auto mb-1"
                ) if filename == '*' else None,
                css_class='align-items-end'
            ),
            Row(
                Column('title', css_class="col-md"),
                Column('target', css_class="col-md"),
                css_class='align-items-end'
            ),
            Row(
                Column('stack_method', css_class="col-md"),
                Column('stack_subtract_bg', css_class="col-md-auto mb-2"),
                Column('stack_mask_cosmics', css_class="col-md-auto mb-2"),
                css_class='align-items-end'
            ) if filename == '*' else None,
            Row(
                Column('ext', css_class="col-md-3"),
                css_class='align-items-end'
            ) if filename != '*' else None,
            Row(
                Column(HTML("自动运行:"), css_class="col-md-auto mb-1"),
                Column('do_inspect', css_class="col-md-auto"),
                Column('do_photometry', css_class="col-md-auto"),
                Column('do_simple_transients', css_class="col-md-auto"),
                Column('do_subtraction', css_class="col-md-auto"),
                css_class='align-items-end justify-content-start'
            ),
        )

        # Populate presets
        self.fields['preset'].choices = [('','')] + [(_.id, _.name) for _ in models.Preset.objects.all()]


class TasksFilterForm(forms.Form):
    query = forms.CharField(max_length=100, required=False, label="筛选任务")
    show_all = forms.BooleanField(initial=False, required=False, label="显示全部")

    def __init__(self, *args, show_all=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'GET'
        self.helper.form_action = 'tasks'
        self.helper.form_show_labels = False
        self.helper.layout = Layout(
            Row(
                Column(
                    InlineField(
                        PrependedText('query', '筛选:', placeholder='按文件名、标题或用户名搜索任务，或指定场中心（可选半径）进行位置搜索。'),
                    ),
                    css_class="col-md"
                ),
                Column(
                    InlineField('show_all'),
                    css_class="col-md-auto mt-2"
                ) if show_all else None,
            )
        )


class TasksActionsForm(forms.Form):
    tasks = MultipleChoiceFieldNoValidation(required=False, widget=forms.CheckboxSelectMultiple)
    referer = forms.CharField(widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.form_action = 'tasks_actions'
        self.helper.layout = Layout()


class PrettyJSONEncoder(json.JSONEncoder):
    def __init__(self, *args, indent, sort_keys, **kwargs):
        super().__init__(*args, indent=4, sort_keys=False, **kwargs)


class TaskInspectForm(forms.Form):
    form_type = forms.CharField(initial='inspect', widget=forms.HiddenInput())
    target = forms.CharField(required=False, empty_value=None, label="目标名称或坐标",
                             widget=forms.Textarea(attrs={'rows':1, 'placeholder': '名称、坐标或 x=... y=... 像素位置，每行一个'}))
    time = forms.CharField(max_length=30, required=False, empty_value=None, label="时间")
    gain = forms.FloatField(min_value=0, required=False, label="增益, e/ADU")
    saturation = forms.FloatField(min_value=0, required=False, label="饱和值, ADU")
    mask_cosmics = forms.BooleanField(initial=False, required=False, label="宇宙线掩模")

    raw_config = forms.JSONField(initial=False, required=False, label="原始配置 JSON", encoder=PrettyJSONEncoder)

    run_photometry = forms.BooleanField(initial=False, required=False, label="测光")
    run_subtraction = forms.BooleanField(initial=False, required=False, label="模板相减")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            'form_type',
            Row(
                Column('target', css_class="col-md-5"),
                Column('time', css_class="col-md-3"),
                Column('gain', css_class="col-md-2"),
                Column('saturation', css_class="col-md-2"),
                css_class='align-items-end'
            ),
            Row(
                Column('mask_cosmics', css_class="col-md-auto"),
                Column(
                    Row(
                        Column(HTML('同时运行:'), css_class="col-md-auto"),
                        Column('run_photometry', css_class="col-md-auto"),
                        Column('run_subtraction', css_class="col-md-auto"),
                        css_class='align-items-start justify-content-start'
                    ),
                    css_class="col-md-auto"
                ),
                css_class='align-items-end justify-content-between'
            ),
        )


class TaskPhotometryForm(forms.Form):
    form_type = forms.CharField(initial='photometry', widget=forms.HiddenInput())
    sn = forms.FloatField(min_value=0, required=False, label="信噪比 S/N")
    initial_aper = forms.FloatField(min_value=0, required=False, label="初始孔径, 像素")
    initial_r0 = forms.FloatField(min_value=0, required=False, label="平滑核, 像素")
    bg_size = forms.IntegerField(min_value=0, required=False, label="背景网格大小")
    minarea = forms.IntegerField(min_value=0, required=False, label="最小目标面积")
    rel_aper = forms.FloatField(min_value=0, initial=1.5, required=False, label="相对孔径, FWHM")
    rel_bg1 = forms.FloatField(min_value=0, required=False, label="天光内环, FWHM")
    rel_bg2 = forms.FloatField(min_value=0, required=False, label="外环, FWHM")
    fwhm_override = forms.FloatField(min_value=0, required=False, label="FWHM 覆盖值, 像素")

    filter = forms.ChoiceField(choices=[('','')] + [(_,supported_filters[_]['name']) for _ in supported_filters.keys()],
                               required=False, label="滤光片")
    cat_name = forms.ChoiceField(choices=[('','')] + [(_,supported_catalogs[_]['name']) for _ in supported_catalogs.keys()],
                                required=False, label="参考星表")
    cat_limit = forms.FloatField(required=False, label="星表极限星等")

    spatial_order = forms.IntegerField(min_value=0, required=False, label="零点空间阶数")
    use_color = forms.BooleanField(required=False, label="使用颜色项")
    sr_override = forms.FloatField(min_value=0, required=False, label="匹配半径, 角秒")

    prefilter_detections = forms.BooleanField(initial=True, required=False, label="预过滤检测")
    filter_blends = forms.BooleanField(initial=True, required=False, label="过滤星表混合源")
    diagnose_color = forms.BooleanField(initial=False, required=False, label="颜色项诊断")
    refine_wcs = forms.BooleanField(required=False, label="精化天体测量")
    blind_match_wcs = forms.BooleanField(required=False, label="盲匹配")
    inspect_bg = forms.BooleanField(required=False, label="检查背景")
    centroid_targets = forms.BooleanField(required=False, label="目标质心化")
    optimal_extraction = forms.BooleanField(required=False, label="最优提取")
    nonlin = forms.BooleanField(required=False, label="非线性")

    blind_match_ps_lo = forms.FloatField(initial=0.2, min_value=0, required=False, label="比例下限, 角秒/像素")
    blind_match_ps_up = forms.FloatField(initial=4.0, min_value=0, required=False, label="比例上限, 角秒/像素")
    blind_match_center = forms.CharField(required=False, empty_value=None, label="盲匹配中心位置")
    blind_match_sr0 = forms.FloatField(initial=2, min_value=0, required=False, label="半径, 度")

    run_subtraction = forms.BooleanField(initial=False, required=False, label="模板相减")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            'form_type',
            Row(
                Column('sn'),
                Column('initial_aper'),
                Column('initial_r0'),
                Column('bg_size'),
                Column('minarea'),
                css_class='align-items-end'
            ),
            Row(
                Column('rel_aper'),
                Column('rel_bg1'),
                Column('rel_bg2'),
                Column('fwhm_override'),
                Column('sr_override'),
                css_class='align-items-end'
            ),
            Row(
                Column('filter'),
                Column('cat_name'),
                Column('cat_limit', css_class="col-md-2"),
                Column('spatial_order', css_class="col-md-2"),
                Column('use_color', css_class="col-md-2 mb-2"),
                css_class='align-items-end'
            ),
            Row(
                Column('blind_match_ps_lo', css_class="col-md-3"),
                Column('blind_match_ps_up', css_class="col-md-3"),
                Column('blind_match_center', css_class="col-md-4"),
                Column('blind_match_sr0', css_class="col-md-2"),
                id='blind_match_params_row',
                css_class='align-items-end'
            ),
            Row(
                Column('refine_wcs', css_class="col-md-auto"),
                Column('blind_match_wcs', css_class="col-md-auto"),
                Column('filter_blends', css_class="col-md-auto"),
                Column('prefilter_detections', css_class="col-md-auto"),
                Column('centroid_targets', css_class="col-md-auto"),
                Column('optimal_extraction', css_class="col-md-auto"),
                Column('nonlin', css_class="col-md-auto"),
                Column('diagnose_color', css_class="col-md-auto"),
                Column('inspect_bg', css_class="col-md-auto"),
                css_class='align-items-end'
            ),
            Row(
                Column(HTML('同时运行:'), css_class="col-md-auto"),
                # Column('run_photometry', css_class="col-md-auto"),
                Column('run_subtraction', css_class="col-md-auto"),
                css_class='align-items-start justify-content-end'
            ),
        )


class TaskTransientsSimpleForm(forms.Form):
    form_type = forms.CharField(initial='transients_simple', widget=forms.HiddenInput())
    # simple_vizier = forms.MultipleChoiceField(
    #     initial=['ps1', 'skymapper'],
    #     choices=[(_,supported_catalogs_transients[_]['name']) for _ in supported_catalogs_transients.keys()],
    #     required=False,
    #     label="Vizier catalogues",
    #     widget=s2forms.Select2MultipleWidget,
    # )
    simple_skybot = forms.BooleanField(initial=True, required=False, label="检查 SkyBoT")
    simple_others = forms.CharField(initial=None, empty_value=None, required=False, label="交叉核对的任务 ID")
    simple_center = forms.CharField(required=False, empty_value=None, label="限制搜索的中心位置")
    simple_sr0 = forms.FloatField(initial=None, min_value=0, required=False, label="半径, 度")
    simple_blends = forms.BooleanField(initial=True, required=False, label="剔除混合源")
    simple_prefilter = forms.BooleanField(initial=True, required=False, label="剔除预过滤源")
    simple_saturated = forms.BooleanField(initial=True, required=False, label="保留饱和源")
    simple_color_term = forms.BooleanField(initial=True, required=False, label="使用颜色项")
    simple_mag_diff = forms.FloatField(initial=2, min_value=0, required=False, label="最小星等差")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            'form_type',
            Row(
                Column('simple_center', css_class="col-md-4"),
                Column('simple_sr0', css_class="col-md-1"),
                Column('simple_mag_diff', css_class="col-md-2"),
                # Column('simple_vizier', css_class="col-md-4"),
                Column('simple_others', css_class="col-md-5"),
                css_class='align-items-end'
            ),
            Row(
                Column('simple_skybot', css_class="col-md-auto"),
                Column('simple_blends', css_class="col-md-auto"),
                Column('simple_prefilter', css_class="col-md-auto"),
                Column('simple_saturated', css_class="col-md-auto"),
                Column('simple_color_term', css_class="col-md-auto"),
            ),
        )


class TaskSubtractionForm(forms.Form):
    form_type = forms.CharField(initial='subtraction', widget=forms.HiddenInput())
    template = forms.ChoiceField(choices=[(_,supported_templates[_]['name']) for _ in supported_templates.keys()],
                                 required=False, label="模板")
    hotpants_extra = forms.JSONField(initial={'ko': 2, 'bgo': 2}, required=False, label="HOTPANTS 附加参数", widget=forms.TextInput)
    sub_size = forms.IntegerField(min_value=0, required=False, label="子图大小")
    sub_overlap = forms.IntegerField(min_value=0, required=False, label="子图重叠")
    sub_verbose = forms.BooleanField(required=False, label="详细输出")
    custom_template = forms.FileField(required=False, label="自定义模板文件")
    template_fwhm_override = forms.FloatField(min_value=0, required=False, label="模板 FWHM 覆盖值, 图像像素")
    custom_template_gain = forms.FloatField(min_value=0, required=False, label="自定义模板增益, e/ADU")
    custom_template_saturation = forms.FloatField(min_value=0, required=False, label="饱和值, ADU")

    subtraction_mode = forms.ChoiceField(choices=[('target', '目标测光'), ('detection', '暂现源检测')],
                                         initial='detection', required=True, label="", widget=forms.RadioSelect)

    subtraction_method = forms.ChoiceField(choices=[('hotpants', 'HOTPANTS'), ('sfft', 'SFFT')],
                                         initial='hotpants', required=False, label="方法")

    sfft_kernel_poly_order = forms.IntegerField(min_value=0, max_value=4, initial=0, required=False, label="核多项式阶数")
    sfft_bg_poly_order = forms.IntegerField(min_value=0, max_value=4, initial=0, required=False, label="背景多项式阶数")
    sfft_flux_poly_order = forms.IntegerField(min_value=0, max_value=4, initial=0, required=False, label="流量多项式阶数")

    filter_vizier = forms.BooleanField(initial=False, required=False, label="过滤 Vizier 星表")
    filter_skybot = forms.BooleanField(initial=False, required=False, label="过滤 SkyBoT")
    filter_prefilter = forms.BooleanField(initial=True, required=False, label="预过滤")
    filter_adjust = forms.BooleanField(initial=True, required=False, label="亚像素调整")
    filter_center = forms.CharField(required=False, empty_value=None, label="限制搜索的中心位置")
    filter_sr0 = forms.FloatField(initial=1, min_value=0, required=False, label="半径, 度")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            'form_type',
            Row(
                Column('template', css_class="col-md-auto"),
                # Column('file'),
                Column('sub_size', css_class="col-md"),
                Column('sub_overlap', css_class="col-md"),
                Column('subtraction_method', css_class="col-md"),
                Column('template_fwhm_override', css_class="col-md"),
                Column('hotpants_extra', id='hotpants_extra_col'),
                Column('sfft_kernel_poly_order', css_class="col-md", id='sfft_kernel_poly_col'),
                Column('sfft_bg_poly_order', css_class="col-md", id='sfft_bg_poly_col'),
                Column('sfft_flux_poly_order', css_class="col-md", id='sfft_flux_poly_col'),
                css_class='align-items-end'
            ),
            Row(
                Column('custom_template'),
                Column('custom_template_gain', css_class="col-md-2"),
                Column('custom_template_saturation', css_class="col-md-2"),
                Column(
                    Submit('action_custom_mask', '制作模板掩模', css_class='btn-secondary mb-1'),
                    css_class="col-md-auto"
                ),
                css_class='align-items-end',
                id='custom_row',
            ),
            Row(
                Column('filter_center', css_class="col-md-4"),
                Column('filter_sr0', css_class="col-md-1"),
                Column('filter_vizier', css_class="col-md-auto mb-2"),
                Column('filter_skybot', css_class="col-md-auto mb-2"),
                Column('filter_prefilter', css_class="col-md-auto mb-2"),
                Column('filter_adjust', css_class="col-md-auto mb-2"),
                css_class='align-items-end',
                id='transients_row',
            ),
            Row(
                Column(InlineRadios('subtraction_mode', template='crispy_radioselect_inline.html'), css_class='form-group'),
                Div(css_class="col-md"),
                Column('sub_verbose', css_class="col-md-1"),
                css_class='align-items-end'
            ),
        )


class SkyPortalUploadForm(forms.Form):
    ids = forms.CharField(
        max_length=150, required=False, label="要上传的任务 ID",
        widget=forms.TextInput(attrs={'placeholder': '要上传的任务 ID 列表，逗号或空格分隔'})
    )
    types = forms.ChoiceField(choices=[
        ('best', '最佳'), ('direct', '直接'), ('subtracted', '模板相减'),
    ],  initial='best', required=False, label="测光类型")
    instrument = forms.ChoiceField(choices=[], initial=None, required=False, label="仪器")
    limit_only = forms.BooleanField(initial=False, required=False, label="仅上传上限")

    def __init__(self, *args, **kwargs):
        instruments = kwargs.pop('instruments')
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            Row(
                Column(
                    'ids',
                    css_class="col-md"
                ),
                Column(
                    'types',
                    css_class="col-md-auto"
                ),
                Column(
                    'instrument',
                    css_class="col-md-auto"
                ),
                Column(
                    Submit('preview', '预览', css_class='btn-primary mb-1'),
                    css_class="col-md-auto"
                ),
                css_class='align-items-end',
            ),
            Row(
                Column('limit_only', css_class="col-md"),
                css_class='align-items-end',
            ),
        )

        if instruments is not None:
            self.fields['instrument'].choices = instruments


class AJSTSelectForm(forms.Form):
    ids = forms.CharField(
        max_length=150, required=False, label="要上传的任务 ID",
        widget=forms.TextInput(attrs={'placeholder': '任务 ID 列表，逗号或空格分隔，支持 a-b 范围'})
    )
    types = forms.MultipleChoiceField(
        choices=[('direct', '直接'), ('subtracted', '模板相减')],
        initial=['direct', 'subtracted'], required=False, label="测光类型",
        widget=forms.CheckboxSelectMultiple,
    )
    transient_id = forms.CharField(
        max_length=150, required=False, label="AJST 源 ID",
        widget=forms.TextInput(attrs={'placeholder': '留空则按坐标解析'})
    )
    create_if_missing = forms.BooleanField(initial=False, required=False, label="源不存在时新建")
    new_t0 = forms.DateTimeField(
        required=False, label="新源 t0（UTC）",
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            Row(
                Column(
                    'ids',
                    css_class="col-md"
                ),
                Column(
                    'types',
                    css_class="col-md-auto"
                ),
                Column(
                    Submit('preview', '预览', css_class='btn-primary mb-1'),
                    css_class="col-md-auto"
                ),
                css_class='align-items-end',
            ),
            Row(
                Column('transient_id', css_class="col-md"),
                Column('create_if_missing', css_class="col-md-auto"),
                Column('new_t0', css_class="col-md-auto"),
                css_class='align-items-end',
            ),
        )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get('create_if_missing') and not cleaned_data.get('new_t0'):
            self.add_error('new_t0', '新建源时必须填写 t0')
        return cleaned_data


class LightcurveSearchForm(forms.Form):
    coordinates = forms.CharField(
        max_length=200,
        required=True,
        label="天空位置",
        widget=forms.TextInput(attrs={'placeholder': '目标名称或坐标'}),
    )
    extra = forms.CharField(
        max_length=200,
        required=False,
        label="附加条件",
        widget=forms.TextInput(attrs={'placeholder': '按文件名、标题、用户名或用户组筛选'}),
    )
    radius = forms.FloatField(min_value=0, initial=5, required=True, label="搜索半径, 角秒")
    show_images = forms.BooleanField(initial=True, required=False, label="显示图像")
    targets_only = forms.BooleanField(initial=True, required=False, label="仅目标测光")
    show_all = forms.BooleanField(initial=True, required=False, label="所有用户的任务")

    def __init__(self, *args, show_all=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'GET'
        self.helper.form_action = 'lightcurves'
        self.helper.field_template = 'crispy_field.html'
        self.helper.layout = Layout(
            Row(
                Column('coordinates', css_class="col-md"),
                Column('extra', css_class="col-md-4"),
                Column('radius', css_class="col-md-2"),
                Column(Submit('search', '搜索', css_class='btn-primary mb-1'), css_class="col-md-auto"),
                css_class='align-items-end',
            ),
            Row(
                Column('show_images', css_class="col-md-auto"),
                Column('targets_only', css_class="col-md-auto"),
                Column('show_all', css_class="col-md-auto") if show_all else None,
                css_class='align-items-end',
            )
        )
