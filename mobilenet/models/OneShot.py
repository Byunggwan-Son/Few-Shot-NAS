import torch.nn as nn
from models.operations import Select_one_OP, OPS 


class SuperNet(nn.Module):
    def __init__(self, search_space, affine, track_running_stats, n_class=1000, input_size=224, width_mult=1.):
        super(SuperNet, self).__init__()

        self.interverted_residual_setting = [
            # channel, layers, stride
            [32,  4, 2],
            [56,  4, 2],
            [112, 4, 2],
            [128, 4, 1],
            [256, 4, 2],
            [432, 1, 1],
        ]

        input_channel = int(40 * width_mult)
        first_cell_width = int(24 * width_mult)

        self.first_conv = nn.Sequential(
            nn.Conv2d(3, input_channel, 3, 2, 1, bias=False),
            nn.BatchNorm2d(input_channel, affine=affine, track_running_stats=track_running_stats),
            nn.ReLU6(inplace=True),
        )

        self.first_block = OPS['3x3_MBConv1'](input_channel, first_cell_width, 1, affine, track_running_stats)
        input_channel = first_cell_width

        self.blocks = nn.ModuleList()
        for c, n, s in self.interverted_residual_setting:
            output_channel = int(c * width_mult)
            for i in range(n):
                if i == 0:
                    self.blocks.append( Select_one_OP(search_space, input_channel, output_channel, s, affine, track_running_stats) )
                else:
                    self.blocks.append( Select_one_OP(search_space, input_channel, output_channel, 1, affine, track_running_stats) )
                input_channel = output_channel

        last_channel = int(1728 * width_mult)
        self.feature_mix_layer = nn.Sequential(
            nn.Conv2d(input_channel, last_channel, 1, 1, 0, bias=False),
            nn.BatchNorm2d(last_channel, affine=affine, track_running_stats=track_running_stats),
            nn.ReLU6(inplace=True)
        ) 
        self.avgpool = nn.AvgPool2d(input_size//32)
        self.classifier = nn.Sequential(
            nn.Linear(last_channel, n_class),
        )


#        for m in self.modules():
#            if isinstance(m, nn.Conv2d):
#                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
#                m.weight.data.normal_(0, math.sqrt(2. / n))
#
#            if isinstance(m, nn.BatchNorm2d):
#                m.weight.data.fill_(1)
#                m.bias.data.zero_()
#                m.affine = True

    def forward(self, x, arch):
        x = self.first_conv(x)
        x = self.first_block(x)

        for ops, op_ind in zip(self.blocks, arch):
            x = ops[op_ind](x)
        x = self.feature_mix_layer(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x
