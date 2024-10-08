from __future__ import annotations

from functools import cached_property, wraps
from typing import Any, Callable, TypeVar

from vsexprtools import norm_expr
from vskernels import Bilinear, BorderHandling, Hermite, Kernel, KernelT, Point, Scaler, ScalerT
from vskernels.types import LeftShift, TopShift
from vsmasktools import KirschTCanny, based_diff_mask
from vsmasktools.utils import _get_region_expr
from vstools import (
    ColorRange, DitherType, FieldBased, FieldBasedT, check_variable, core, depth, get_peak_value,
    get_y, join, split, vs
)

from .helpers import BottomCrop, CropRel, LeftCrop, RightCrop, ScalingArgs, TopCrop
from .onnx import ArtCNN

RescaleT = TypeVar('RescaleT', bound="RescaleBase")


class RescaleBase:
    descale_args: ScalingArgs
    field_based: FieldBased | None

    def __init__(
        self,
        clip: vs.VideoNode,
        /,
        kernel: KernelT,
        upscaler: ScalerT = ArtCNN,
        downscaler: ScalerT = Hermite(linear=True),
        field_based: FieldBasedT | None = None,
        border_handling: int | BorderHandling = BorderHandling.MIRROR
    ) -> None:
        assert check_variable(clip, self.__class__)

        self.clipy, *chroma = split(clip)
        self.chroma = chroma

        self.kernel = Kernel.ensure_obj(kernel)
        self.upscaler = Scaler.ensure_obj(upscaler)

        self.downscaler = Scaler.ensure_obj(downscaler)

        self.field_based = FieldBased.from_param(field_based)

        self.border_handling = BorderHandling(int(border_handling))

    def __delattr__(self, __name: str) -> None:
        match __name:
            case 'descale':
                self._trydelattr('rescale')
                self._trydelattr('doubled')
            case 'doubled':
                self._trydelattr('upscale')
            case _:
                pass
        delattr(self, __name)

    def _trydelattr(self, attr: str) -> None:
        try:
            delattr(self, attr)
        except AttributeError:
            pass

    @staticmethod
    def _apply_field_based(function: Callable[[RescaleT, vs.VideoNode], vs.VideoNode]) -> Callable[[RescaleT, vs.VideoNode], vs.VideoNode]:
        @wraps(function)
        def wrap(self: RescaleT, clip: vs.VideoNode) -> vs.VideoNode:
            if self.field_based:
                clip = self.field_based.apply(clip)
                clip = function(self, clip)
                return FieldBased.PROGRESSIVE.apply(clip)
            else:
                return function(self, clip)
        return wrap

    @staticmethod
    def _add_props(function: Callable[[RescaleT, vs.VideoNode], vs.VideoNode]) -> Callable[[RescaleT, vs.VideoNode], vs.VideoNode]:
        @wraps(function)
        def wrap(self: RescaleT, clip: vs.VideoNode) -> vs.VideoNode:
            w, h = (
                f"{int(d)}" if d.is_integer() else f"{d:.2f}"
                for d in [self.descale_args.src_width, self.descale_args.src_height]
            )
            return function(self, clip).std.SetFrameProp(
                "Rescale" + function.__name__.split('_')[-1].capitalize() + 'From',
                data=f'{self.kernel.__class__.__name__} - {w} x {h}'
            )
        return wrap

    @_add_props
    @_apply_field_based
    def _generate_descale(self, clip: vs.VideoNode) -> vs.VideoNode:
        return self.kernel.descale(
            clip,
            self.descale_args.width, self.descale_args.height,
            **self.descale_args.kwargs(),
            border_handling=self.border_handling
        )

    @_add_props
    @_apply_field_based
    def _generate_rescale(self, clip: vs.VideoNode) -> vs.VideoNode:
        return self.kernel.scale(
                clip,
                self.clipy.width, self.clipy.height,
                **self.descale_args.kwargs(),
                border_handling=self.border_handling
        )

    @_add_props
    def _generate_doubled(self, clip: vs.VideoNode) -> vs.VideoNode:
        return self.upscaler.multi(clip, 2)

    @_add_props
    def _generate_upscale(self, clip: vs.VideoNode) -> vs.VideoNode:
        return self.downscaler.scale(
            clip,
            self.clipy.width, self.clipy.height,
            **self.descale_args.kwargs(clip)
        )

    @cached_property
    def descale(self) -> vs.VideoNode:
        return self._generate_descale(self.clipy)

    @cached_property
    def rescale(self) -> vs.VideoNode:
        return self._generate_rescale(self.descale)

    @cached_property
    def doubled(self) -> vs.VideoNode:
        return self._generate_doubled(self.descale)

    @cached_property
    def upscale(self) -> vs.VideoNode:
        """Returns the upscaled clip"""
        return join(
            self._generate_upscale(self.doubled),
            *self.chroma
        )


class Rescale(RescaleBase):
    """
    _docstring_
    """

    def __init__(
        self,
        clip: vs.VideoNode,
        /,
        height: int | float,
        kernel: KernelT,
        upscaler: ScalerT = ArtCNN,
        downscaler: ScalerT = Hermite(linear=True),
        width: int | float | None = None,
        base_height: int | None = None,
        base_width: int | None = None,
        crop: tuple[LeftCrop, RightCrop, TopCrop, BottomCrop] = CropRel(),
        shift: tuple[TopShift, LeftShift] = (0, 0),
        field_based: FieldBasedT | None = None,
        border_handling: int | BorderHandling = BorderHandling.MIRROR
    ) -> None:
        """Initialize the rescaling process.

        :param clip:                Clip to be rescaled
        :param height:              Height to be descaled to. Forcing the value to float will ensure a fractionnal descale
        :param kernel:              Kernel used for descaling
        :param upscaler:            Scaler that supports doubling, defaults to ArtCNN
        :param downscaler:          Scaler used for downscaling the upscaled clip back to input res, defaults to Hermite(linear=True)
        :param width:               Width to be descaled to. If None, automatically calculated from the height
        :param base_height:         Integer height at which the clip will be contained. If None, automatically calculated from the height
        :param base_width:          Integer width at which the clip will be contained. If None, automatically calculated from the width
        :param crop:                Cropping values to apply before descale. The ratio descale height / source height will be preserved even after descale.
                                    The cropped area is restored when calling the `upscale` property.
        :param shift:               Shifts to apply during descale and upscale, defaults to (0, 0)
        :param field_based:         Parameter specifying the source is a cross-converted/interlaced upscaled content
        :param border_handling:     Adjust the way the clip is padded internally during the scaling process.
                                    Accepted values are:
                                        0: Assume the image was resized with mirror padding.
                                        1: Assume the image was resized with zero padding.
                                        2: Assume the image was resized with extend padding, where the outermost row was extended infinitely far.
                                    Defaults to 0
        """
        self._line_mask: vs.VideoNode | None = None
        self._credit_mask: vs.VideoNode | None = None
        self._ignore_mask: vs.VideoNode | None = None
        self._crop = crop
        self._pre = clip

        self.descale_args = ScalingArgs.from_args(clip, height, width, base_height, base_width, shift[0], shift[1], crop, mode='hw')

        super().__init__(clip, kernel, upscaler, downscaler, field_based, border_handling)

        if self._crop > (0, 0, 0, 0):
            self.clipy = self.clipy.std.Crop(*self._crop)

    def _generate_descale(self, clip: vs.VideoNode) -> vs.VideoNode:
        if not self._ignore_mask:
            return super()._generate_descale(clip)

        @self._add_props
        @self._apply_field_based
        def _generate_descale_ignore_mask(self: Rescale, clip: vs.VideoNode) -> vs.VideoNode:
            assert self._ignore_mask

            self.descale_args.mode = 'h'

            descale_h = self.kernel.descale(
                clip,
                None, self.descale_args.height,
                **self.descale_args.kwargs(),
                border_handling=self.border_handling,
                ignore_mask=self._ignore_mask
            )

            self.descale_args.mode = 'w'

            descale_w = self.kernel.descale(
                descale_h,
                self.descale_args.width, None,
                **self.descale_args.kwargs(),
                border_handling=self.border_handling,
                ignore_mask=Point.scale(self._ignore_mask, height=descale_h.height)
            )

            self.descale_args.mode = 'hw'

            return descale_w

        return _generate_descale_ignore_mask(self, clip)

    def _generate_upscale(self, clip: vs.VideoNode) -> vs.VideoNode:
        upscale = super()._generate_upscale(clip)

        if self._line_mask or self.border_handling:
            upscale = core.std.MaskedMerge(self.clipy, upscale, self.line_mask).std.CopyFrameProps(upscale)

        if self._credit_mask:
            upscale = core.std.MaskedMerge(upscale, self.clipy, self.credit_mask)

        if self._crop > (0, 0, 0, 0):
            pre_y = get_y(self._pre)
            black = pre_y.std.BlankClip()
            mask = norm_expr(black, _get_region_expr(black, *self._crop, replace=f'{get_peak_value(black, False, ColorRange.FULL)} x'))

            upscale = core.std.MaskedMerge(upscale.std.AddBorders(*self._crop), pre_y, mask)

        return upscale

    @property
    def line_mask(self) -> vs.VideoNode:
        lm  = self._line_mask or self.clipy.std.BlankClip(color=get_peak_value(self.clipy, False, ColorRange.FULL))

        if self.border_handling:
            px = (self.kernel.kernel_radius, ) * 4
            lm = norm_expr(lm, _get_region_expr(lm, *px, replace=f'{get_peak_value(lm, False, ColorRange.FULL)} x'))

        self._line_mask = lm

        return self._line_mask

    @line_mask.setter
    def line_mask(self, mask: vs.VideoNode | None) -> None:
        self._line_mask = depth(mask, self.clipy, dither_type=DitherType.NONE) if mask else mask

    @line_mask.deleter
    def line_mask(self) -> None:
        self._line_mask = None

    @property
    def credit_mask(self) -> vs.VideoNode:
        if self._credit_mask:
            return self._credit_mask
        self.credit_mask = self.clipy.std.BlankClip()
        return self.credit_mask

    @credit_mask.setter
    def credit_mask(self, mask: vs.VideoNode | None) -> None:
        self._credit_mask = depth(mask, self.clipy, dither_type=DitherType.NONE) if mask else mask

    @credit_mask.deleter
    def credit_mask(self) -> None:
        self._credit_mask = None

    @property
    def ignore_mask(self) -> vs.VideoNode:
        if self._ignore_mask:
            return self._ignore_mask
        self.ignore_mask = self.clipy.std.BlankClip(format=vs.GRAY8)
        return self.ignore_mask

    @ignore_mask.setter
    def ignore_mask(self, mask: vs.VideoNode | None) -> None:
        self._ignore_mask = depth(mask, 8, dither_type=DitherType.NONE) if mask else mask

    @ignore_mask.deleter
    def ignore_mask(self) -> None:
        self._ignore_mask = None

    def default_line_mask(self, clip: vs.VideoNode | None = None, scaler: ScalerT = Bilinear, **kwargs: Any) -> vs.VideoNode:
        """
        Load a default Kirsch line mask in the class instance. Additionnaly, it is returned.

        :param clip:    Reference clip, defaults to luma source clip if None.
        :param scaler:  Scaled used for matching the source clip format, defaults to Bilinear
        :return:        Generated mask.
        """
        line_mask = KirschTCanny.edgemask(clip if clip else self.clipy, **kwargs).std.Maximum().std.Minimum()
        line_mask = Scaler.ensure_obj(scaler).scale(line_mask, self.clipy.width, self.clipy.height, format=self.clipy.format)

        self.line_mask = line_mask

        return self.line_mask

    def default_credit_mask(
        self, rescale: vs.VideoNode | None = None, src: vs.VideoNode | None = None,
        thr: float = 0.216, expand: int = 4, **kwargs: Any
    ) -> vs.VideoNode:
        """
        Load a credit mask by making a difference mask between src and rescaled clips

        :param rescale:     Rescaled clip, defaults to rescaled instance clip
        :param src:         Source clip, defaults to source instance clip
        :param thr:         Threshold of the amplification expr, defaults to 0.216
        :param expand:      Additional expand radius applied to the mask, defaults to 4
        :return:            Generated mask
        """
        if not src:
            src = self.clipy
        if not rescale:
            rescale = self.rescale

        src, rescale = get_y(src), get_y(rescale)

        self.credit_mask = based_diff_mask(src, rescale, thr=thr, expand=expand, func=self.default_credit_mask, **kwargs)

        return self.credit_mask