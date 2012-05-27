
class AdminViewMixin(object):

    def __init__(self, **kwargs):
        super(AdminViewMixin, self).__init__(**kwargs)
        self.model = self.admin_opts.model
        self.model_opts = self.model._meta

    def get_queryset(self):
        return self.admin_opts.queryset(self.request)